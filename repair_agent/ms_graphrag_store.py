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

    documents = collect_documents()
    inp = _input_dir()
    # Clean slate so a rebuild reflects the CURRENT on-disk code (mirrors the other backends' reset).
    if inp.exists():
        import shutil
        shutil.rmtree(inp, ignore_errors=True)
    inp.mkdir(parents=True, exist_ok=True)

    meta: dict = {}
    for i, doc in enumerate(documents):
        name = f"chunk_{i:05d}.txt"
        (inp / name).write_text(doc.page_content, encoding="utf-8")
        m = dict(doc.metadata)
        meta[name] = {
            "source": m.get("source", ""),
            "type": m.get("type", ""),
            "start_line": m.get("start_line"),
            "end_line": m.get("end_line"),
            "section": m.get("section"),
        }
    (_root() / _META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    return meta


def _scaffold_settings():
    """Ensure <root>/settings.yaml exists (scaffold it for the INSTALLED GraphRAG version), then PATCH
    only the model wiring to point at the local Ollama model + embeddings. Scaffolding via `graphrag init`
    guarantees the file matches whatever schema the installed version expects; we edit as little as
    possible so we stay compatible across releases."""
    yaml = _need("yaml")
    root = _root()
    settings_path = root / "settings.yaml"
    if not settings_path.exists():
        # `graphrag init` writes settings.yaml + prompts + .env for the installed version.
        _run_graphrag(["init", "--root", str(root)], check=False)
    if not settings_path.exists():
        # Fallback minimal settings if init didn't produce one (older/newer CLI shapes).
        settings_path.write_text(_MINIMAL_SETTINGS, encoding="utf-8")

    cfg = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}

    chat = {
        "type": "openai_chat",
        "api_base": _api_base(),
        "api_key": settings.graphrag_api_key or "ollama",
        "model": _llm_model(),
        "model_supports_json": False,
        "concurrent_requests": 1,
        "async_mode": "threaded",
        "requests_per_minute": 0,
        "tokens_per_minute": 0,
    }
    embed = {
        "type": "openai_embedding",
        "api_base": _api_base(),
        "api_key": settings.graphrag_api_key or "ollama",
        "model": settings.graphrag_embedding_model,
        "concurrent_requests": 1,
        "async_mode": "threaded",
        "requests_per_minute": 0,
        "tokens_per_minute": 0,
    }

    # v2 schema: a `models:` map referenced by name. v1 schema: top-level `llm:` + `embeddings.llm:`.
    if isinstance(cfg.get("models"), dict) and cfg["models"]:
        for name, spec in cfg["models"].items():
            if not isinstance(spec, dict):
                continue
            is_embed = "embed" in name.lower() or spec.get("type", "").endswith("embedding")
            spec.update(embed if is_embed else chat)
    else:
        cfg["llm"] = {**cfg.get("llm", {}), **chat}
        cfg.setdefault("embeddings", {})
        cfg["embeddings"]["llm"] = {**cfg["embeddings"].get("llm", {}), **embed}

    # Point input at our per-chunk .txt files and set the text-unit size. Keep other keys as scaffolded.
    cfg["input"] = {**cfg.get("input", {}), "type": "file", "file_type": "text",
                    "base_dir": _INPUT_SUBDIR, "file_pattern": r".*\.txt$"}
    cfg.setdefault("chunks", {})
    cfg["chunks"]["size"] = int(settings.graphrag_chunk_size)
    cfg["chunks"].setdefault("overlap", 100)

    settings_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


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

    Writes per-chunk inputs + a metadata sidecar, scaffolds/patches settings.yaml to use the local
    Ollama model + embeddings, then runs `graphrag index`. The heavy work (entity/relationship
    extraction, community detection + summarisation) is the LLM's — it is SLOW on CPU and meant for a
    code model on a GPU. Idempotent: safe to re-run to reflect the current on-disk code."""
    root = _root()
    root.mkdir(parents=True, exist_ok=True)
    meta = _write_inputs()
    if not meta:
        print("No valid chunks found to index (msgraphrag).")
        return 0

    print(f"Extracted {len(meta)} chunks. Building the Microsoft GraphRAG graph with "
          f"'{_llm_model()}' via {_api_base()} (this is LLM-heavy and slow on CPU)…")
    _scaffold_settings()
    _run_graphrag(["index", "--root", str(root)], check=True)
    _TU_CACHE.clear()

    n = _count_text_units()
    print(f"Microsoft GraphRAG index complete: {n} text units + entity/community graph at {_output_dir()}.")
    return n or len(meta)


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
        if first is not None:
            title = id_to_title.get(str(first))
            if title:
                m = dict(title_meta.get(Path(title).name, {}))
        texts.append(text)
        meta.append({
            "source": m.get("source", ""),
            "type": m.get("type", "code_block"),
            "start_line": m.get("start_line"),
            "end_line": m.get("end_line"),
            "section": m.get("section"),
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
    return Document(page_content=cache["texts"][i], metadata=dict(cache["meta"][i]))


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
    picked = [i for i in order if type_ok(meta[i])][:k]
    results = [_doc(i, cache) for i in picked]

    # Blend in the graph's community summaries only for an unfiltered query (retrieve_context's main
    # code/general passes), never for the type-filtered code-only or source passes — keeps those pure.
    if not where:
        results += _community_docs(query_text, k=2)
    return results


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


# Minimal fallback settings.yaml if `graphrag init` is unavailable in the installed version (patched by
# _scaffold_settings before use). Uses the v2 `models:` schema.
_MINIMAL_SETTINGS = """\
models:
  default_chat_model:
    type: openai_chat
    model: placeholder
  default_embedding_model:
    type: openai_embedding
    model: placeholder
input:
  type: file
  file_type: text
  base_dir: input
  file_pattern: ".*\\\\.txt$"
chunks:
  size: 1200
  overlap: 100
embed_text:
  model_id: default_embedding_model
extract_graph:
  model_id: default_chat_model
community_reports:
  model_id: default_chat_model
"""
