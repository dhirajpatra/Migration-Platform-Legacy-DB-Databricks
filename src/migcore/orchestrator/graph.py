from __future__ import annotations
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from migcore.agents import pipeline as P
from migcore.connectors.registry import build_connector


class RunState(TypedDict, total=False):
    run_id: str
    run_dir: str
    status: str                       # running | blocked | awaiting_g1 | g1_approved | g1_rejected
    issues: Annotated[list, operator.add]
    discovery: dict
    ast: dict
    graph: dict
    assessment: dict
    gate1: dict


def build_graph(cfg: dict, connector_factory=build_connector, checkpointer=None):
    g = StateGraph(RunState)
    g.add_node("guardrail", lambda s: P.guardrail(s, cfg, connector_factory))
    g.add_node("discovery", lambda s: P.discovery(s, cfg, connector_factory))
    g.add_node("ast", lambda s: P.ast_preprocess(s, cfg))
    g.add_node("graph_build", lambda s: P.graph_build(s, cfg))
    g.add_node("analyze", lambda s: P.analyze_node(s, cfg))

    def gate1(s: RunState) -> dict:
        # Human-in-the-loop Gate 1 (scope & waves). Run pauses here until someone resumes it.
        decision: dict[str, Any] = interrupt({"gate": "G1", "assessment_file": str(Path(s["run_dir"]) / "assessment.md"),
                                              "summary": s.get("assessment", {})})
        approved = bool(decision.get("approved"))
        P.audit(Path(s["run_dir"]), decision.get("approver", "unknown"), "gate1_approved" if approved else "gate1_rejected",
                {"comment": decision.get("comment", "")})
        return {"gate1": decision, "status": "g1_approved" if approved else "g1_rejected"}

    g.add_node("gate1", gate1)
    g.add_edge(START, "guardrail")
    g.add_conditional_edges("guardrail", lambda s: "stop" if s.get("status") == "blocked" else "go",
                            {"stop": END, "go": "discovery"})
    g.add_edge("discovery", "ast")
    g.add_edge("ast", "graph_build")
    g.add_edge("graph_build", "analyze")
    g.add_edge("analyze", "gate1")
    g.add_edge("gate1", END)  # milestone 1 continues from here: target design -> translation
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer)
