"""
GraphRAG (Neo4j-backed) retrieval for Auto-Repair — the LOCAL, data-sovereign alternative to the
default Chroma + HuggingFace store.

Selected with settings.repair_retrieval_backend == "graphrag". Everything runs INSIDE the customer's
environment: the SAME local HuggingFace sentence-transformer embeddings as the Chroma path are stored
as a vector index in Neo4j, alongside a lightweight CODE GRAPH — each chunk is linked to the File it
came from (Chunk-[:PART_OF]->File) — so retrieval combines vector similarity with a graph query
(file-neighbourhood expansion is a graph traversal, not a second vector scan). No code or documents
leave the box; pair with REPAIR_LLM_BACKEND=local (Ollama + a lightweight Llama such as llama3.2:3b)
for a fully in-house repair with nothing sent to a third-party service.

This is the practical, runnable form of graph-RAG on Neo4j. The heavier Microsoft GraphRAG
entity/community-summarisation pipeline (which needs an LLM to BUILD the graph and, for Llama-4-class
models, a GPU) can populate the SAME Neo4j store later without changing this retrieval seam.

Public API mirrors parse_code_and_store so the switch is transparent to retrieve_context:
    build_index() -> int          # (re)build the Neo4j vector index + code graph
    search(query, k, where)       # -> list[langchain Document] with identical metadata keys

Dependencies (`langchain-neo4j`, `neo4j`) are imported LAZILY and only here, so the default Chroma
backend carries no new dependency and nothing changes unless graphrag is actually selected.
"""
from vision_agent.config import settings

INDEX_NAME = "repair_chunks"
NODE_LABEL = "RepairChunk"
TEXT_PROP = "text"
EMBEDDING_PROP = "embedding"


# ── lazy imports ────────────────────────────────────────────────────────────────
def _import_neo4jvector():
    """Neo4jVector from langchain-neo4j (preferred) or the older langchain_community location."""
    try:
        from langchain_neo4j import Neo4jVector
        return Neo4jVector
    except Exception:
        pass
    try:
        from langchain_community.vectorstores import Neo4jVector  # older layout
        return Neo4jVector
    except Exception as e:
        raise ImportError(
            "The graphrag retrieval backend needs the [graphrag] extra. Install it with "
            "`pip install -e \".[graphrag]\"` (langchain-neo4j + neo4j) and run a Neo4j server."
        ) from e


def _driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))


def _run(cypher: str, params: dict | None = None):
    drv = _driver()
    try:
        with drv.session(database=settings.neo4j_database) as session:
            return list(session.run(cypher, params or {}))
    finally:
        drv.close()


def _conn_kwargs() -> dict:
    return dict(
        url=settings.neo4j_uri,
        username=settings.neo4j_user,
        password=settings.neo4j_password,
        database=settings.neo4j_database,
        index_name=INDEX_NAME,
        node_label=NODE_LABEL,
        text_node_property=TEXT_PROP,
        embedding_node_property=EMBEDDING_PROP,
    )


def _embeddings():
    # Reuse the SAME cached local HuggingFace model as the Chroma path — embeddings are computed
    # in-process and never sent anywhere, which is the whole point of the local backend.
    from repair_agent.parse_code_and_store import _get_embedding_model
    return _get_embedding_model()


# ── build ────────────────────────────────────────────────────────────────────────
def build_index() -> int:
    """(Re)build the Neo4j vector index + code graph from the same chunks the Chroma path indexes."""
    from repair_agent.parse_code_and_store import collect_documents

    documents = collect_documents()
    if not documents:
        print("No valid chunks found to index (graphrag/Neo4j).")
        return 0

    print(f"Extracted {len(documents)} searchable chunks. Building Neo4j graph-RAG index…")

    # Clean slate: drop the vector index and the prior chunk/file nodes so a rebuild reflects the
    # CURRENT on-disk code (mirrors the Chroma path's in-place reset). Batched delete keeps memory low.
    try:
        _run(f"DROP INDEX {INDEX_NAME} IF EXISTS")
    except Exception as e:
        print(f"  (could not drop old vector index, continuing: {e})")
    _run(f"MATCH (c:{NODE_LABEL}) CALL {{ WITH c DETACH DELETE c }} IN TRANSACTIONS OF 500 ROWS")
    _run("MATCH (f:File) CALL { WITH f DETACH DELETE f } IN TRANSACTIONS OF 500 ROWS")

    Neo4jVector = _import_neo4jvector()
    Neo4jVector.from_documents(documents, _embeddings(), **_conn_kwargs())

    # Add the lightweight CODE GRAPH: link every chunk to the File it came from. This is what makes
    # file-neighbourhood expansion (see search) a graph traversal, and is the seam a richer
    # entity/community graph can extend later.
    _run(
        f"MATCH (c:{NODE_LABEL}) WHERE c.source IS NOT NULL "
        f"MERGE (f:File {{path: c.source}}) MERGE (c)-[:PART_OF]->(f)"
    )
    print(f"Neo4j graph-RAG index complete: {len(documents)} chunks + File graph at {settings.neo4j_uri}.")
    return len(documents)


# ── search ─────────────────────────────────────────────────────────────────────
def _normalize_filter(where: dict | None) -> dict | None:
    """Translate the codebase's Chroma-style filters to Neo4jVector operator filters.
    Only three shapes are used by retrieve_context: {'type': {'$in':[...]}}, {'type': 'x'}, {'source': 'x'}."""
    if not where:
        return None
    out = {}
    for key, val in where.items():
        out[key] = val if isinstance(val, dict) else {"$eq": val}
    return out


def _docs_for_source(src: str, k: int):
    """Graph fetch: ALL chunks belonging to one File node — the file-neighbourhood expansion that
    retrieve_context relies on (search(where={'source': src}, k=60)). A pure graph query, no vectors."""
    from langchain_core.documents import Document
    rows = _run(
        f"MATCH (c:{NODE_LABEL} {{source: $src}}) RETURN properties(c) AS props LIMIT $k",
        {"src": src, "k": int(k)},
    )
    docs = []
    for r in rows:
        props = dict(r["props"])
        text = props.pop(TEXT_PROP, "")
        props.pop(EMBEDDING_PROP, None)
        docs.append(Document(page_content=text or "", metadata=props))
    return docs


def search(query_text, k=4, where=None):
    """Vector search over the Neo4j index (+ optional metadata filter), or a graph fetch when the
    caller asks for all chunks of a single source file. Returns langchain Documents with the same
    metadata keys as the Chroma path so retrieve_context is unchanged."""
    # File-neighbourhood expansion → graph traversal (retrieve_context calls this with a large k).
    if where and set(where.keys()) == {"source"} and isinstance(where.get("source"), str):
        return _docs_for_source(where["source"], k)

    Neo4jVector = _import_neo4jvector()
    store = Neo4jVector.from_existing_index(_embeddings(), **_conn_kwargs())
    flt = _normalize_filter(where)
    try:
        if flt:
            return store.similarity_search(query_text, k=k, filter=flt)
        return store.similarity_search(query_text, k=k)
    except Exception as e:
        # Robust fallback across langchain-neo4j versions: over-fetch unfiltered, then post-filter in
        # Python on the same metadata shapes. Keeps retrieval working even if native filtering differs.
        print(f"  [graphrag] native filter failed ({e}); falling back to post-filter.")
        pool = store.similarity_search(query_text, k=max(k * 8, 40))
        if not where:
            return pool[:k]
        out = [d for d in pool if _meta_matches(d.metadata, where)]
        return out[:k]


def index_exists() -> bool:
    """True if the Neo4j graph-RAG index holds any chunks. Cheap node count; best-effort so a
    down/unreachable Neo4j reports 'not built' rather than raising. Used by the index-status endpoint
    (the graphrag backend has no local persist dir to stat, unlike Chroma)."""
    try:
        rows = _run(f"MATCH (c:{NODE_LABEL}) RETURN count(c) AS n")
        return bool(rows) and int(rows[0]["n"]) > 0
    except Exception:
        return False


def _meta_matches(meta: dict, where: dict) -> bool:
    for key, cond in where.items():
        val = meta.get(key)
        if isinstance(cond, dict):
            if "$in" in cond and val not in cond["$in"]:
                return False
            if "$eq" in cond and val != cond["$eq"]:
                return False
        elif val != cond:
            return False
    return True
