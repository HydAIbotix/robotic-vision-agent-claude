"""
Auto-Repair Agent — LangGraph StateGraph (the self-healing arm of defect intelligence).

TWO-AGENT flow (2026-09-21): a distinct RCA agent runs FIRST, then the code-fixing agent.

  rca → retrieve → diagnose → (guard) → apply → unit_test → build → prepare_pr → END
    └─ spec_bug / test_invalid (RCA stop) ────────────────────────────────────────→ END
                   └─ dry-run ──────────────────────────────────────────────────────→ END
                              └─ repo blocked ─────────────────────────────────────→ END

  • rca      — the RCA agent: reads ONLY design/requirements docs + the test case, decides code_bug /
               spec_bug / test_invalid. A high-confidence spec/test verdict STOPS here (no code is
               patched to satisfy a broken spec or wrong test); a code bug localises + proceeds.
  • retrieve — the code-fixing agent's CODE retrieval (efficient code-only vector RAG), seeded by RCA.
  • diagnose — the code-fixing agent's ONE model call (Claude or local) → a minimal patch.

Pure `state -> dict` nodes with conditional routing, matching the other four agents. Only the RCA and
DIAGNOSE steps call a model; retrieval stays Chroma/HuggingFace (+ msgraphrag for docs). The thin
`run_repair()` wrapper in repair_failed_test.py drives this graph.
"""
from langgraph.graph import StateGraph, END

from repair_agent.state import RepairAgentState
from repair_agent.nodes.rca import rca_node
from repair_agent.nodes.retrieve import retrieve_node
from repair_agent.nodes.diagnose import diagnose_node
from repair_agent.nodes.apply import guard_node, apply_node
from repair_agent.nodes.unit_test import unit_test_node
from repair_agent.nodes.build import build_node
from repair_agent.nodes.prepare_pr import prepare_pr_node


def _route_after_rca(state: RepairAgentState) -> str:
    # A high-confidence spec_bug / test_invalid halts before the fixer — nothing gets patched.
    return "end" if state.get("rca_stop") else "retrieve"


def _route_after_diagnose(state: RepairAgentState) -> str:
    # apply=False → a dry run: stop after producing the diagnosis (retrieve + patch only).
    return "guard" if state.get("apply", True) else "end"


def _route_after_guard(state: RepairAgentState) -> str:
    # A dirty repo (mid-merge/rebase/conflicts) stops here so no branch gets corrupted.
    return "end" if state.get("blocked") else "apply"


def create_repair_agent():
    g = StateGraph(RepairAgentState)

    g.add_node("rca",        rca_node)
    g.add_node("retrieve",   retrieve_node)
    g.add_node("diagnose",   diagnose_node)
    g.add_node("guard",      guard_node)
    g.add_node("apply",      apply_node)
    g.add_node("unit_test",  unit_test_node)
    g.add_node("build",      build_node)
    g.add_node("prepare_pr", prepare_pr_node)

    g.set_entry_point("rca")
    g.add_conditional_edges("rca", _route_after_rca, {"retrieve": "retrieve", "end": END})
    g.add_edge("retrieve", "diagnose")
    g.add_conditional_edges("diagnose", _route_after_diagnose, {"guard": "guard", "end": END})
    g.add_conditional_edges("guard",    _route_after_guard,    {"apply": "apply", "end": END})
    g.add_edge("apply",     "unit_test")
    g.add_edge("unit_test", "build")
    g.add_edge("build",     "prepare_pr")
    g.add_edge("prepare_pr", END)

    return g.compile()
