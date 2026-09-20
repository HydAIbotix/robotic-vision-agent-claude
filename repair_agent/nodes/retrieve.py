"""RETRIEVE node — pull the offending code from the active RAG backend.

No Claude here. Delegates to the existing, tested `retrieve_context`, which dispatches to the
configured retrieval backend (Chroma + HuggingFace by default, or GraphRAG + Neo4j locally). The
active backend name is streamed to the UI (`tool`) so the stage sub-label reflects what actually ran.
"""
from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import retrieve_context, retrieval_tool_label
from repair_agent.state import RepairAgentState


def retrieve_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    tool = retrieval_tool_label()
    emit(rid, "retrieve", "running", tool=tool)
    # The code-fixing agent retrieves CODE, seeded by the RCA agent's localisation terms (rca_query).
    context, hits = retrieve_context(state["failure"], rca_query=state.get("rca_query", ""))
    emit(rid, "retrieve", "done", hits=hits, tool=tool)
    stages = {**state.get("stages", {}), "retrieve": {"status": "done", "hits": hits, "tool": tool}}
    return {"context": context, "hits": hits, "stages": stages}
