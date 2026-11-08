from __future__ import annotations
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt
from migcore.agents import pipeline as P
from migcore.connectors.registry import build_connector
from migcore.cloud.databricks import DatabricksClient


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
    design: dict
    gate2: dict
    translation: dict
    verification: dict


def build_graph(cfg: dict, connector_factory=build_connector, checkpointer=None, databricks=None):
    db = databricks or DatabricksClient.from_env(cfg)
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
    g.add_node("target_design", lambda s: P.target_design_node(s, cfg))

    def gate2(s: RunState) -> dict:
        # Human-in-the-loop Gate 2 (target design: naming, layout, constraints policy).
        decision: dict[str, Any] = interrupt({"gate": "G2", "design_file": str(Path(s["run_dir"]) / "target" / "design.json"),
                                              "summary": s.get("design", {})})
        approved = bool(decision.get("approved"))
        P.audit(Path(s["run_dir"]), decision.get("approver", "unknown"), "gate2_approved" if approved else "gate2_rejected",
                {"comment": decision.get("comment", "")})
        return {"gate2": decision, "status": "g2_approved" if approved else "g2_rejected"}

    g.add_node("gate2", gate2)
    g.add_node("translate", lambda s: P.translate_node(s, cfg))
    g.add_node("verify", lambda s: P.verify_node(s, cfg, db))
    g.add_edge(START, "guardrail")
    g.add_conditional_edges("guardrail", lambda s: "stop" if s.get("status") == "blocked" else "go",
                            {"stop": END, "go": "discovery"})
    g.add_edge("discovery", "ast")
    g.add_edge("ast", "graph_build")
    g.add_edge("graph_build", "analyze")
    g.add_edge("analyze", "gate1")
    g.add_conditional_edges("gate1", lambda s: "go" if s.get("status") == "g1_approved" else "stop",
                            {"go": "target_design", "stop": END})
    g.add_edge("target_design", "gate2")
    g.add_conditional_edges("gate2", lambda s: "go" if s.get("status") == "g2_approved" else "stop",
                            {"go": "translate", "stop": END})
    g.add_edge("translate", "verify")
    g.add_edge("verify", END)   # milestone 2 continues from here: code/trigger translation
    if checkpointer is None:
        from langgraph.checkpoint.memory import MemorySaver
        checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer)
