"""RETRIEVE node — pull the offending code from the Chroma RAG index (Chroma + HuggingFace).

No Claude here. Delegates to the existing, tested `retrieve_context` so behaviour is identical to
the pre-LangGraph pipeline.
"""
from repair_agent import canceller
from repair_agent.broadcaster import emit
from repair_agent.repair_failed_test import retrieve_context
from repair_agent.state import RepairAgentState


def retrieve_node(state: RepairAgentState) -> dict:
    rid = state.get("repair_id", "")
    canceller.bail_if_cancelled(rid)
    emit(rid, "retrieve", "running")
    context, hits = retrieve_context(state["failure"])
    emit(rid, "retrieve", "done", hits=hits)
    stages = {**state.get("stages", {}), "retrieve": {"status": "done", "hits": hits}}
    return {"context": context, "hits": hits, "stages": stages}
