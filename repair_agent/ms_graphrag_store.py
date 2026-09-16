"""
Microsoft GraphRAG retrieval for Auto-Repair — the REAL entity/community graph pipeline.

Selected with settings.repair_retrieval_backend == "msgraphrag". Unlike the Neo4j graph-RAG option
(graphrag_store.py, a vector index + a lightweight file graph), THIS backend runs the genuine
Microsoft GraphRAG indexing pipeline: a LOCAL LLM reads the code + product docs and EXTRACTS entities
and relationships, clusters them into communities (Leiden) and writes community summaries, producing a
knowledge graph (parquet artifacts + a LanceDB vector store) in a workspace directory. Retrieval then
draws on the graph's TEXT UNITS (the chunked source) plus the most relevant COMMUNITY REPORTS (the
graph's synthesised "big picture"), so DIAGNOSE gets both the offending code and the spec-level intent.

ONE MODEL for build AND fix: GraphRAG's indexing LLM is pointed at the SAME Ollama model that
DIAGNOSES the repair (settings.repair_local_model, via Ollama's OpenAI-compatible endpoint), so there
is a single local model for both retrieval-graph building and code fixing — nothing leaves the box.

Public API mirrors parse_code_and_store / graphrag_store so the switch is transparent to
retrieve_context:
    build_index() -> int          # run the Microsoft GraphRAG pipeline over the collected chunks
    search(query, k, where)       # -> list[langchain Document] with identical metadata keys
    index_exists() -> bool        # have the GraphRAG output artifacts been produced?

`graphrag`, `pandas`, `numpy`, `yaml` are imported LAZILY and only here (the new [msgraphrag] extra),
so the default Chroma backend (and the graphrag/Neo4j backend) carry no new dependency and nothing
changes unless msgraphrag is actually selected. The pipeline is heavy and reasoning-driven — meant for
a code model (qwen2.5-coder) and, for the larger sizes, a GPU.

Design note: this module is intentionally VERSION-TOLERANT. Microsoft GraphRAG's settings schema and
output filenames have changed across releases, so we (a) let `graphrag init` scaffold the correct
settings.yaml for the INSTALLED version and only PATCH the model wiring, and (b) load whichever output
parquet names exist. Retrieval re-embeds the text units with the same local HuggingFace model the other
backends use (cached), which keeps search fast and independent of LanceDB's evolving API.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from vision_agent.config import settings

# Metadata sidecar: maps each input document (one file per collected chunk) → its real source/type/lines,
# so retrieval can hand DIAGNOSE the same metadata keys the Chroma/Neo4j backends do. GraphRAG records
# each input file's name as the document `title`, which is how we join back to this map.
_META_FILE = "metadata.json"
_INPUT_SUBDIR = "input"

# In-process cache of the loaded text units + their embeddings, so repeated searches in one repair don't
# re-read parquet or re-embed. Invalidated when the output dir's mtime changes (a rebuild).
_TU_CACHE: dict = {}


# ── paths & config resolution ─────────────────────────────────────────────────────
def _root() -> Path:
    from repair_agent.parse_code_and_store import _resolve
    return _resolve(settings.graphrag_root_dir)


def _input_dir() -> Path:
    return _root() / _INPUT_SUBDIR


def _output_dir() -> Path:
    # GraphRAG writes final parquet artifacts to <root>/output (v1/v2). We only READ from here.
    return _root() / "output"


def _llm_model() -> str:
    """The chat model GraphRAG uses to BUILD the graph — the SAME model that fixes the code by default."""
    return settings.graphrag_llm_model or settings.repair_local_model


def _api_base() -> str:
    """Ollama's OpenAI-compatible endpoint (…/v1). Defaults off the local-LLM base URL so one setting
    (REPAIR_LOCAL_BASE_URL) drives both the diagnose model and the graph-building model."""
    base = settings.graphrag_api_base or (settings.repair_local_base_url.rstrip("/") + "/v1")
    return base


# ── lazy imports (clear, actionable errors) ────────────────────────────────────────
def _need(module: str):
    try:
        return __import__(module)
    except Exception as e:  # pragma: no cover - environment-dependent
        raise ImportError(
            "The 'msgraphrag' retrieval backend needs the [msgraphrag] extra "
            "(graphrag + pandas + pyyaml). Install it with `pip install -e \".[msgraphrag]\"` "
            "(or rebuild the image with --build-arg INSTALL_MSGRAPHRAG=true) and run an Ollama server "
            f"with the model + embedding model pulled. Missing: {module}."
        ) from e


def _embeddings():
    # Reuse the SAME cached local HuggingFace model as the other backends for RETRIEVAL similarity — kept
    # local, and fast (loaded once per process). GraphRAG's own embedding model (graphrag_embedding_model)
    # is used only while BUILDING the graph; search re-embeds text units here for version-independence.
    from repair_agent.parse_code_and_store import _get_embedding_model
    return _get_embedding_model()


# ── build ──────────────────────────────────────────────────────────────────────────
def _write_inputs() -> dict:
    """Write each collected chunk as its own input file and return the {filename: metadata} sidecar map.

    One file per chunk keeps the source/lines mapping intact through GraphRAG's own processing (GraphRAG
    stores each input file's name as the document title, which we join back on at search time)."""
    from repair_agent.parse_code_and_store import collect_documents

    import hashlib
    documents = collect_documents()
    inp = _input_dir()
    # Clean slate so the input dir reflects EXACTLY the current chunks. Filenames are CONTENT hashes
    # (below), so identical content lands at the identical filename → GraphRAG's `update` recognises an
    # unchanged document and skips re-extracting it; only edited/added chunks get new names/ids. Wiping
    # here is safe for incremental because `update` diffs against the OUTPUT parquet (which we never
    # delete), not against the input folder.
    if inp.exists():
        import shutil
        shutil.rmtree(inp, ignore_errors=True)
    inp.mkdir(parents=True, exist_ok=True)

    meta: dict = {}
    for doc in documents:
        m = dict(doc.metadata)
        # Line-INDEPENDENT identity: hash source + text (not line numbers), so a function that only
        # shifted lines after an edit elsewhere keeps the same id and is not needlessly re-extracted.
        key = (str(m.get("source", "")) + "\n" + doc.page_content).encode("utf-8")
        name = hashlib.md5(key).hexdigest()[:20] + ".txt"
        (inp / name).write_text(doc.page_content, encoding="utf-8")
        meta[name] = {
            "source": m.get("source", ""),
            "type": m.get("type", ""),
            "start_line": m.get("start_line"),
            "end_line": m.get("end_line"),
            "section": m.get("section"),
        }
    (_root() / _META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _default_template() -> str:
    """GraphRAG's OWN default settings.yaml text (so we always match the installed version's schema).
    We deliberately do NOT run `graphrag init`: in GraphRAG 3.x it is INTERACTIVE (prompts for the model
    and aborts on EOF), which silently left us on a wrong hand-written fallback. Reading the package's
    INIT_YAML template gives the exact schema with zero prompts."""
    try:
        from graphrag.config.init_content import INIT_YAML
        return INIT_YAML
    except Exception:
        return _MINIMAL_SETTINGS


def _scaffold_settings():
    """Write a complete, version-matched settings.yaml wired to the local Ollama model + embeddings.

    Start from GraphRAG's default template (INIT_YAML), then: point BOTH model groups at the same local
    Ollama model via its OpenAI-compatible endpoint; set the plain-TEXT input reader over our per-chunk
    .txt files (in GraphRAG 3.x `input.type` is the reader = 'text', and the folder lives under a SEPARATE
    `input_storage.base_dir`; no file_pattern is needed — the text reader defaults to .txt, which also
    sidesteps the '$' end-anchor Template issue); and DROP the prompt-file paths so GraphRAG falls back to
    its BUILT-IN default prompts (we don't run init, so there is no prompts/ dir to load from)."""
    yaml = _need("yaml")
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    settings_path = root / "settings.yaml"

    cfg = yaml.safe_load(_default_template()) or {}
    api_base = _api_base()
    api_key = settings.graphrag_api_key or "ollama"

    # Both the completion (chat) model and the embedding model → the local Ollama server. The completion
    # model is the SAME one that diagnoses the fix (_llm_model → repair_local_model), so a single local
    # model builds the graph AND fixes the code. Overriding api_key here also removes the template's
    # ${GRAPHRAG_API_KEY} placeholder, so no .env is required.
    for group, model_name in (("completion_models", _llm_model()),
                              ("embedding_models", settings.graphrag_embedding_model)):
        models = cfg.get(group) or {}
        for _mid, spec in models.items():
            if isinstance(spec, dict):
                spec["model_provider"] = "openai"     # Ollama exposes an OpenAI-compatible API
                spec["model"] = model_name
                spec["auth_method"] = "api_key"
                spec["api_key"] = api_key
                spec["api_base"] = api_base
        cfg[group] = models

    # Input = plain-text reader over our chunk files (type is the READER; storage/base_dir is separate).
    cfg.setdefault("input", {})["type"] = "text"
    cfg.setdefault("input_storage", {"type": "file"})["base_dir"] = _INPUT_SUBDIR
    cfg.setdefault("chunking", {})["size"] = int(settings.graphrag_chunk_size)

    # Use GraphRAG's built-in default prompts for MOST stages (no prompts/ dir since we skip init):
    # remove the file paths. extract_graph is handled separately below (POS domain typing).
    for section in ("summarize_descriptions", "community_reports", "extract_claims",
                    "local_search", "global_search", "drift_search", "basic_search"):
        sec = cfg.get(section)
        if isinstance(sec, dict):
            for key in ("prompt", "graph_prompt", "text_prompt", "map_prompt", "reduce_prompt",
                        "knowledge_prompt"):
                sec.pop(key, None)

    # ── POS DOMAIN TYPING (the lever that makes the graph speak POS) ────────────────────────────
    eg = cfg.setdefault("extract_graph", {})
    types = [t.strip() for t in settings.graphrag_entity_types.split(",") if t.strip()]
    if types:
        eg["entity_types"] = types      # GraphRAG's extraction prompt is parameterised by these
    # Optionally add a short POS preamble to GraphRAG's OWN default extraction prompt (keeps the strict
    # tuple format valid). If we can't locate the default template, fall back to the built-in prompt —
    # entity_types alone still make the graph POS-typed.
    if settings.graphrag_domain_prompt and (base := _default_extract_prompt()):
        pdir = root / "prompts"; pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "extract_graph.txt").write_text(_POS_PREAMBLE.rstrip() + "\n\n" + base, encoding="utf-8")
        eg["prompt"] = "prompts/extract_graph.txt"
        print("  [msgraphrag] POS extraction prompt written (entity_types + domain preamble).")
    else:
        eg.pop("prompt", None)
        print(f"  [msgraphrag] POS entity_types applied ({', '.join(types)}); using default extraction prompt.")

    settings_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


# A short domain preamble PREPENDED to GraphRAG's own extraction prompt. It only adds context — the
# strict tuple format + placeholders come from the appended default prompt, so this can't break parsing.
_POS_PREAMBLE = """\
-Domain context-
The text below is source code and design/requirements/test documentation for a POINT-OF-SALE (POS)
kiosk application. It has two kiosk stations: an RPS station where PURCHASES happen, and a VPS
ValuePass station where SMART CARDS are issued/validated and BALANCES are checked. A purchase at RPS
must reduce the card BALANCE and record a PURCHASE TRANSACTION so it is visible when the card is later
checked at VPS. When extracting, prefer the domain entity types provided and capture control/data flow
relationships such as: a Function CALLS another Function; a purchase RECORDS a Transaction; a
Transaction UPDATES a Balance; a KioskStation ISSUES/VALIDATES a SmartCard; an Endpoint/module
PERSISTS data. Then follow the standard instructions below exactly.
"""


def _default_extract_prompt() -> str | None:
    """GraphRAG's own default entity/relationship extraction prompt text, so we can AUGMENT it (keeping
    its exact tuple format) rather than hand-writing one that might not match the installed version.
    Tries the known module locations across GraphRAG 1.x–3.x; returns None if none match (→ we then use
    the built-in prompt with just entity_types)."""
    candidates = [
        ("graphrag.index.operations.extract_graph.graph_extractor_prompt", "GRAPH_EXTRACTION_PROMPT"),
        ("graphrag.prompts.index.extract_graph", "GRAPH_EXTRACTION_PROMPT"),
        ("graphrag.prompts.index.extract_graph", "EXTRACT_GRAPH_PROMPT"),
        ("graphrag.index.graph.extractors.graph.prompts", "GRAPH_EXTRACTION_PROMPT"),
    ]
    for mod, attr in candidates:
        try:
            m = __import__(mod, fromlist=[attr])
            v = getattr(m, attr, None)
            if isinstance(v, str) and "{input_text}" in v and "tuple_delimiter" in v:
                return v
        except Exception:
            continue
    return None


def _run_graphrag(args: list[str], check: bool = True):
    """Invoke the GraphRAG CLI in a version-tolerant way: prefer `python -m graphrag`, fall back to the
    `graphrag` console script. Raises with the captured output on failure so the user can iterate."""
    _need("graphrag")  # ensure importable + a clear error if not
    attempts = [[sys.executable, "-m", "graphrag", *args], ["graphrag", *args]]
    last = None
    for cmd in attempts:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            continue
        last = proc
        if proc.returncode == 0:
            if proc.stdout:
                print(proc.stdout[-2000:])
            return proc
        # `-m graphrag` may not expose a subcommand on some versions → try the console script next.
        if "No module named" in (proc.stderr or "") or "usage:" in (proc.stderr or "").lower():
            continue
        break
    if check and (last is None or last.returncode != 0):
        tail = (last.stderr or last.stdout or "")[-2000:] if last else "graphrag CLI not found"
        raise RuntimeError(f"GraphRAG command failed ({' '.join(args)}):\n{tail}")
    return last


def build_index() -> int:
    """Run the Microsoft GraphRAG pipeline over the collected chunks and return the number indexed.

    Writes per-chunk inputs + a metadata sidecar, scaffolds settings.yaml (local Ollama model +
    embeddings), then runs the pipeline. INCREMENTAL by default: if a graph already exists and
    settings.graphrag_incremental is on, run GraphRAG's `update` (re-extract only NEW/CHANGED docs and
    merge) instead of a full `index`; otherwise a full `index`. The heavy work (entity/relationship
    extraction, community detection + summarisation) is the LLM's — SLOW, meant for a code model on a GPU.
    Idempotent: safe to re-run to reflect the current on-disk code."""
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    meta = _write_inputs()
    if not meta:
        print("No valid chunks found to index (msgraphrag).")
        return 0

    _scaffold_settings()
    # Full `index` on the first build (nothing to diff against); `update` (incremental) afterwards. The
    # first build establishes the OUTPUT parquet that `update` diffs the current inputs against.
    incremental = settings.graphrag_incremental and index_exists()
    op = "update" if incremental else "index"
    print(f"Extracted {len(meta)} chunks. Running Microsoft GraphRAG '{op}' "
          f"({'incremental — only new/changed docs' if incremental else 'full build'}) with "
          f"'{_llm_model()}' via {_api_base()} (LLM-heavy; slow on CPU)…")
    _run_graphrag([op, "--root", str(root)], check=True)
    _TU_CACHE.clear()
    _WHOLE_CACHE.clear()

    n = _count_text_units()
    print(f"Microsoft GraphRAG index complete: {n} text units + entity/community graph at {_output_dir()}.")
    # Also load the graph into Neo4j so it is browsable in the Neo4j Browser (best-effort, never fatal).
    _export_to_neo4j()
    return n or len(meta)


def _export_to_neo4j() -> None:
    """Load the built graph (entities + relationships + community summaries) into Neo4j so it is
    BROWSABLE in the Neo4j Browser (http://<host>:7474 · bolt://…:7687). Best-effort: any failure
    (Neo4j down, unexpected parquet columns) logs a warning and NEVER fails the expensive index build.
    Uses labels Entity/Community + a RELATED edge, distinct from the graphrag(Neo4j) RAG store's
    RepairChunk/File, so both can coexist in the same database."""
    if not settings.graphrag_export_neo4j:
        return
    try:
        from repair_agent.graphrag_store import _run   # reuses the NEO4J_* connection settings
    except Exception as e:
        print(f"  [msgraphrag] Neo4j export skipped (neo4j driver unavailable: {e})")
        return

    ents = _load_df("entities")
    if ents is None or len(ents) == 0:
        print("  [msgraphrag] no entities parquet found — skipping Neo4j export.")
        return
    rels = _load_df("relationships")
    reports = _load_df("community_reports")

    def _col(df, *names):
        for nm in names:
            if nm in df.columns:
                return nm
        return None

    try:
        # Fresh slate for the EXPORTED graph only (leave the RAG store's RepairChunk/File untouched).
        _run("MATCH (n:Entity) DETACH DELETE n")
        _run("MATCH (n:Community) DETACH DELETE n")

        name_c, type_c, desc_c = _col(ents, "title", "name"), _col(ents, "type"), _col(ents, "description")
        erows = []
        for _, r in ents.iterrows():
            nm = str(r.get(name_c) or "").strip()
            if nm:
                erows.append({"name": nm,
                              "type": str(r.get(type_c) or "") if type_c else "",
                              "description": str(r.get(desc_c) or "") if desc_c else ""})
        _run("UNWIND $rows AS row MERGE (e:Entity {name: row.name}) "
             "SET e.type = row.type, e.description = row.description", {"rows": erows})

        rel_n = 0
        if rels is not None and len(rels):
            s_c, t_c = _col(rels, "source"), _col(rels, "target")
            rd_c, w_c = _col(rels, "description"), _col(rels, "weight")
            rrows = []
            for _, r in rels.iterrows():
                s, t = str(r.get(s_c) or "").strip(), str(r.get(t_c) or "").strip()
                if not s or not t:
                    continue
                try:
                    w = float(r.get(w_c)) if w_c and r.get(w_c) is not None else 1.0
                except Exception:
                    w = 1.0
                rrows.append({"s": s, "t": t, "weight": w,
                              "description": str(r.get(rd_c) or "") if rd_c else ""})
            _run("UNWIND $rows AS row MERGE (a:Entity {name: row.s}) MERGE (b:Entity {name: row.t}) "
                 "MERGE (a)-[x:RELATED]->(b) SET x.description = row.description, x.weight = row.weight",
                 {"rows": rrows})
            rel_n = len(rrows)

        com_n = 0
        if reports is not None and len(reports):
            id_c, lvl_c = _col(reports, "community", "id"), _col(reports, "level")
            tit_c, sum_c = _col(reports, "title"), _col(reports, "summary", "full_content")
            crows = []
            for _, r in reports.iterrows():
                cid = r.get(id_c)
                if cid is None:
                    continue
                try:
                    lvl = int(r.get(lvl_c)) if lvl_c and r.get(lvl_c) is not None else 0
                except Exception:
                    lvl = 0
                crows.append({"id": str(cid), "level": lvl,
                              "title": str(r.get(tit_c) or "") if tit_c else "",
                              "summary": str(r.get(sum_c) or "") if sum_c else ""})
            _run("UNWIND $rows AS row MERGE (c:Community {id: row.id}) "
                 "SET c.level = row.level, c.title = row.title, c.summary = row.summary", {"rows": crows})
            com_n = len(crows)

        print(f"  [msgraphrag] Neo4j export: {len(erows)} entities, {rel_n} relationships, "
              f"{com_n} community reports → {settings.neo4j_uri} (browse in the Neo4j Browser).")
    except Exception as e:
        print(f"  [msgraphrag] Neo4j export failed (non-fatal): {e}")


# ── output artifact loading (version-tolerant) ─────────────────────────────────────
def _find_parquet(*stems: str) -> Path | None:
    out = _output_dir()
    if not out.exists():
        return None
    for stem in stems:
        for cand in (out / f"{stem}.parquet", out / f"create_final_{stem}.parquet"):
            if cand.exists():
                return cand
    return None


def _load_df(*stems: str):
    pd = _need("pandas")
    path = _find_parquet(*stems)
    if path is None:
        return None
    try:
        return pd.read_parquet(path)
    except Exception as e:
        print(f"  [msgraphrag] could not read {path.name}: {e}")
        return None


def _metadata_map() -> dict:
    p = _root() / _META_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _count_text_units() -> int:
    df = _load_df("text_units")
    return 0 if df is None else int(len(df))


def _title_to_meta() -> dict:
    """Map a GraphRAG document title (== our input filename) → the real chunk metadata."""
    return {k: v for k, v in _metadata_map().items()}


def _title_variants(title: str):
    """Filename spellings GraphRAG may store as the document `title` (with/without .txt, path or bare)."""
    if not title:
        return []
    name = Path(title).name
    return [name, name + ".txt", Path(name).stem + ".txt", title]


def _sidecar_lookup(title: str, sidecar: dict) -> dict:
    """Robustly resolve a document title to our metadata sidecar entry across title spellings."""
    for cand in _title_variants(title):
        if cand in sidecar:
            return dict(sidecar[cand])
    return {}


def _chunk_filename(title: str) -> str:
    """The on-disk input filename for a title (the WHOLE original tree-sitter chunk), or ''."""
    for cand in _title_variants(title):
        if (_input_dir() / cand).exists():
            return cand
    return ""


_WHOLE_CACHE: dict = {}


def _whole_chunk_for(chunk_file: str) -> str | None:
    """Read the ORIGINAL whole tree-sitter chunk (full function / doc section) for a hit, so the model
    sees the buggy line and its context together even when GraphRAG split the function into text units.
    Cached per file; returns None if the file is missing (→ caller keeps the text-unit snippet)."""
    if not chunk_file:
        return None
    if chunk_file in _WHOLE_CACHE:
        return _WHOLE_CACHE[chunk_file]
    p = _input_dir() / chunk_file
    try:
        txt = p.read_text(encoding="utf-8") if p.exists() else None
    except Exception:
        txt = None
    _WHOLE_CACHE[chunk_file] = txt
    return txt


def _load_text_units() -> dict:
    """Load text units with their real source metadata and local embeddings, cached per output build.

    Returns {"texts": [...], "meta": [...], "embs": np.ndarray}. Robust to missing columns: if the
    document→title join isn't available, metadata falls back to empty (retrieval still works)."""
    out = _output_dir()
    mtime = out.stat().st_mtime if out.exists() else 0
    if _TU_CACHE.get("dir") == str(out) and _TU_CACHE.get("mtime") == mtime and "embs" in _TU_CACHE:
        return _TU_CACHE

    np = _need("numpy")
    tu = _load_df("text_units")
    if tu is None or len(tu) == 0:
        raise RuntimeError(
            f"Microsoft GraphRAG produced no text units at {out}. Build the index first: "
            f"POST /api/repair/index (or python -m repair_agent.parse_code_and_store)."
        )

    docs = _load_df("documents")
    id_to_title = {}
    if docs is not None and "id" in docs.columns:
        title_col = "title" if "title" in docs.columns else ("name" if "name" in docs.columns else None)
        if title_col:
            id_to_title = dict(zip(docs["id"].astype(str), docs[title_col].astype(str)))
    title_meta = _title_to_meta()

    text_col = "text" if "text" in tu.columns else ("chunk" if "chunk" in tu.columns else None)
    texts, meta = [], []
    for _, row in tu.iterrows():
        text = str(row[text_col]) if text_col else ""
        # text_unit → document_ids[0] → document title (our input filename) → real metadata.
        m = {}
        doc_ids = row.get("document_ids")
        try:
            first = doc_ids[0] if doc_ids is not None and len(doc_ids) else None
        except Exception:
            first = None
        title = id_to_title.get(str(first)) if first is not None else None
        if title:
            m = _sidecar_lookup(title, title_meta)
        texts.append(text)
        meta.append({
            "source": m.get("source", ""),
            "type": m.get("type", "code_block"),
            "start_line": m.get("start_line"),
            "end_line": m.get("end_line"),
            "section": m.get("section"),
            "_chunk_file": _chunk_filename(title),   # the input file = the WHOLE original tree-sitter chunk
        })

    embs = np.array(_embeddings().embed_documents(texts), dtype="float32") if texts else np.zeros((0, 1), "float32")
    _TU_CACHE.clear()
    _TU_CACHE.update({"dir": str(out), "mtime": mtime, "texts": texts, "meta": meta, "embs": embs})
    return _TU_CACHE


def _community_docs(query_text: str, k: int):
    """Top community-report summaries by similarity — the graph's synthesised 'big picture', which is
    what makes Microsoft GraphRAG valuable beyond plain chunks. Best-effort: skipped if absent."""
    from langchain_core.documents import Document
    if k <= 0:
        return []
    df = _load_df("community_reports")
    if df is None or len(df) == 0:
        return []
    np = _need("numpy")
    text_col = next((c for c in ("full_content", "summary", "content", "title") if c in df.columns), None)
    if not text_col:
        return []
    texts = [str(t) for t in df[text_col].tolist() if str(t).strip()]
    if not texts:
        return []
    emb = _embeddings()
    q = np.array(emb.embed_query(query_text), dtype="float32")
    M = np.array(emb.embed_documents(texts), dtype="float32")
    order = _rank(q, M, np)[:k]
    return [Document(page_content=texts[i], metadata={"source": "graphrag://community_report",
                                                      "type": "graphrag_community"}) for i in order]


def _rank(q, M, np):
    if len(M) == 0:
        return []
    qn = q / (np.linalg.norm(q) + 1e-9)
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    return list(np.argsort(-(Mn @ qn)))


# ── search ─────────────────────────────────────────────────────────────────────────
def _doc(i: int, cache) -> "object":
    from langchain_core.documents import Document
    return Document(page_content=cache["texts"][i], metadata=_clean_meta(cache["meta"][i]))


def search(query_text, k=4, where=None):
    """Vector search over the GraphRAG text units (+ optional metadata filter), or a source-fetch when the
    caller asks for all chunks of one file (the file-neighbourhood expansion retrieve_context relies on).
    For a normal query it also appends a couple of the graph's most relevant COMMUNITY REPORTS. Returns
    langchain Documents with the same metadata keys as the other backends so retrieve_context is unchanged."""
    cache = _load_text_units()
    texts, meta = cache["texts"], cache["meta"]

    # File-neighbourhood expansion → return all text units whose real source == src (no vectors needed).
    if where and set(where.keys()) == {"source"} and isinstance(where.get("source"), str):
        src = where["source"]
        return [_doc(i, cache) for i, m in enumerate(meta) if m.get("source") == src][:k]

    np = _need("numpy")
    q = np.array(_embeddings().embed_query(query_text), dtype="float32")
    order = _rank(q, cache["embs"], np)

    # Optional type filter ({"type": {"$in": [...]}} or {"type": "x"}) — post-filter on real metadata.
    type_ok = _type_predicate(where)
    from langchain_core.documents import Document
    results, seen = [], set()
    for i in order:
        if not type_ok(meta[i]):
            continue
        m = meta[i]
        # WHOLE-FUNCTION expansion: return the ORIGINAL tree-sitter chunk (full function/section) instead
        # of GraphRAG's possibly-truncated text unit, so the buggy line + its context stay together. Dedupe
        # by the source chunk so several units of one function collapse to one whole-function hit.
        whole = _whole_chunk_for(m.get("_chunk_file")) if settings.graphrag_whole_function else None
        if whole is not None:
            key = m.get("_chunk_file")
            if key in seen:
                continue
            seen.add(key)
            results.append(Document(page_content=whole, metadata=_clean_meta(m)))
        else:
            key = (m.get("source"), texts[i][:40])
            if key in seen:
                continue
            seen.add(key)
            results.append(_doc(i, cache))
        if len(results) >= k:
            break

    # Blend in the graph's community summaries only for an unfiltered query (retrieve_context's main
    # code/general passes), never for the type-filtered code-only or source passes — keeps those pure.
    if not where:
        results += _community_docs(query_text, k=2)
    return results


def _clean_meta(m: dict) -> dict:
    """Drop internal (_-prefixed) keys so the Document carries only the standard metadata the other
    backends expose (source/type/start_line/end_line/section)."""
    return {k: v for k, v in m.items() if not k.startswith("_")}


def _type_predicate(where):
    if not where or "type" not in where:
        return lambda m: True
    cond = where["type"]
    if isinstance(cond, dict) and "$in" in cond:
        allowed = set(cond["$in"])
        return lambda m: m.get("type") in allowed
    return lambda m, c=cond: m.get("type") == c


def index_exists() -> bool:
    """True if the Microsoft GraphRAG output artifacts (text units) exist. Best-effort so a missing/half
    workspace reports 'not built' rather than raising. Used by the index-status endpoint (this backend has
    no Chroma persist dir and no Neo4j to count)."""
    try:
        return _find_parquet("text_units") is not None and _count_text_units() > 0
    except Exception:
        return False


# Fallback settings.yaml used ONLY if GraphRAG's own INIT_YAML template can't be imported (unexpected
# package layout). Matches the GraphRAG 3.x schema (completion_models/embedding_models, input.type as the
# reader, separate input_storage) and is finalised by _scaffold_settings (model wiring + our chunk dir).
_MINIMAL_SETTINGS = """\
completion_models:
  default_completion_model:
    model_provider: openai
    model: placeholder
embedding_models:
  default_embedding_model:
    model_provider: openai
    model: placeholder
input:
  type: text
input_storage:
  type: file
  base_dir: input
output_storage:
  type: file
  base_dir: output
chunking:
  type: tokens
  size: 1200
  overlap: 100
vector_store:
  type: lancedb
  db_uri: output/lancedb
embed_text:
  embedding_model_id: default_embedding_model
extract_graph:
  completion_model_id: default_completion_model
community_reports:
  completion_model_id: default_completion_model
"""
