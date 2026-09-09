"""
Semantic memory port — the AgentCore-Memory analogue (Explorer screen memory / agent recall).

The App Explorer's durable memory is the app_map; this port adds an OPTIONAL semantic index so an
agent can recall similar screens/flows by meaning (e.g. "have I seen a screen like this before?").

  none    (default) — disabled; remember()/search() are no-ops. Zero overhead, MVP behaviour.
  chroma  — a local Chroma collection (same embeddings as the repair RAG).
  pgvector— a Postgres + pgvector collection (shares the DB; via langchain-postgres).

Both vector backends lazy-import their SDK, so nothing is pulled in until selected. Embeddings reuse
the repair agent's cached HuggingFace model when available. Namespaced by tenant so pooled tenants
never share memory.
"""
from __future__ import annotations

from vision_agent.config import settings

_store = None
_store_backend = None


def enabled() -> bool:
    return settings.memory_backend != "none"


def _embeddings():
    # Reuse the repair agent's cached embedding model if present (avoids a second model load);
    # otherwise construct one. Both use the same HuggingFace sentence-transformers model.
    try:
        from repair_agent.parse_code_and_store import _get_embedding_model  # cached singleton
        return _get_embedding_model()
    except Exception:
        from langchain_huggingface import HuggingFaceEmbeddings
        return HuggingFaceEmbeddings(model_name=settings.memory_embedding_model)


def _get_store():
    """Build (once) the configured vector store, or None when disabled/unavailable."""
    global _store, _store_backend
    if settings.memory_backend == "none":
        return None
    if _store is not None and _store_backend == settings.memory_backend:
        return _store
    try:
        if settings.memory_backend == "pgvector":
            from langchain_postgres import PGVector
            conn = settings.pgvector_url or settings.db_url
            # langchain-postgres wants a psycopg (v3) URL; normalise a bare postgres URL.
            conn = conn.replace("postgresql+psycopg2://", "postgresql+psycopg://")
            _store = PGVector(embeddings=_embeddings(),
                              collection_name=settings.memory_collection,
                              connection=conn, use_jsonb=True)
        else:  # chroma
            from langchain_chroma import Chroma
            _store = Chroma(collection_name=settings.memory_collection,
                            embedding_function=_embeddings(),
                            persist_directory=settings.memory_persist_dir)
        _store_backend = settings.memory_backend
    except Exception as exc:  # pragma: no cover - environment-dependent
        print(f"  [memory] {settings.memory_backend} init failed ({exc}); memory disabled")
        _store = None
    return _store


def _ns(tenant_id: str | None) -> str:
    from ports.tenancy import current_tenant
    return tenant_id or current_tenant()


def remember(text: str, metadata: dict | None = None, tenant_id: str | None = None) -> None:
    """Index a piece of text (e.g. a screen signature/description). No-op when memory is disabled."""
    store = _get_store()
    if store is None or not (text or "").strip():
        return
    meta = {"tenant_id": _ns(tenant_id), **(metadata or {})}
    try:
        store.add_texts([text], metadatas=[meta])
    except Exception as exc:  # pragma: no cover
        print(f"  [memory] remember failed ({exc})")


def search(query: str, k: int = 5, tenant_id: str | None = None) -> list[dict]:
    """Return up to k similar memories for the current tenant: [{text, metadata}]. Empty when disabled."""
    store = _get_store()
    if store is None or not (query or "").strip():
        return []
    try:
        docs = store.similarity_search(query, k=k, filter={"tenant_id": _ns(tenant_id)})
        return [{"text": d.page_content, "metadata": d.metadata} for d in docs]
    except Exception as exc:  # pragma: no cover
        print(f"  [memory] search failed ({exc})")
        return []


def reset() -> None:
    """Test hook — drop the cached store so the next call rebuilds from current config."""
    global _store, _store_backend
    _store = None
    _store_backend = None
