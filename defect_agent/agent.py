"""
Defect Intelligence Sub-Agent — LangGraph sub-graph.

Flow:
  evaluation_node → defect_intelligence_node → result_publisher_node → END

Triggered automatically after a test run completes with one or more failures.
"""
from langgraph.graph import StateGraph, END
from defect_agent.state import DefectAgentState
from defect_agent.nodes.evaluate import evaluation_node
from defect_agent.nodes.defect_intelligence import defect_intelligence_node
from defect_agent.nodes.publish import result_publisher_node


def create_defect_agent():
    g = StateGraph(DefectAgentState)

    g.add_node("evaluation",         evaluation_node)
    g.add_node("defect_intelligence", defect_intelligence_node)
    g.add_node("result_publisher",   result_publisher_node)

    g.set_entry_point("evaluation")
    g.add_edge("evaluation",         "defect_intelligence")
    g.add_edge("defect_intelligence", "result_publisher")
    g.add_edge("result_publisher",   END)

    return g.compile()
