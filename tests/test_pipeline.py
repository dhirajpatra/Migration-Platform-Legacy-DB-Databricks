import json
import pytest
from langgraph.types import Command
from fake_connector import SeedConnector
from migcore.orchestrator.graph import build_graph

SOURCES = [{"id": "mssql_demo", "engine": "sqlserver"}, {"id": "oracle_demo", "engine": "oracle"}]


def _cfg(**kw):
    return {"graph": {"enabled": False}, "sources": SOURCES, **kw}


def _factory(src):
    return SeedConnector(src["id"], src)


def _run(tmp_path, cfg=None, factory=_factory):
    g = build_graph(cfg or _cfg(), factory)
    conf = {"configurable": {"thread_id": "t1"}}
    out = g.invoke({"run_id": "t1", "run_dir": str(tmp_path), "status": "running", "issues": []}, conf)
    return g, conf, out


def test_full_run_pauses_at_gate1_then_approves(tmp_path):
    g, conf, out = _run(tmp_path)
    assert g.get_state(conf).next == ("gate1",)                      # paused for a human
    assert (tmp_path / "assessment.md").exists() and (tmp_path / "graph_payload.json").exists()
    a = json.loads((tmp_path / "assessment.json").read_text())
    assert a["counts"]["Table"] == 10 and a["counts"]["Package"] == 1 and a["counts"]["View"] == 4
    # dependency order: tables and leaf functions come before the procedures that use them
    wave = {o: w["wave"] for w in a["waves"] for o in w["objects"]}
    assert wave["mssql_demo.dbo.fn_ordertotal"] < wave["mssql_demo.dbo.vw_customerorders"] < 99
    assert wave["mssql_demo.dbo.usp_addauditnote"] < wave["mssql_demo.dbo.usp_closemonth"]
    # hard-flag routing: unresolved dynamic SQL forces human review
    assert "mssql_demo.dbo.usp_closemonth" in a["needs_human"]
    assert a["scores"]["mssql_demo.dbo.usp_addauditnote"]["bucket"] == "Simple"

    g.invoke(Command(resume={"approved": True, "approver": "dhiraj"}), conf)
    assert g.get_state(conf).next == ("gate2",)                      # next human gate: target design
    audit = [json.loads(l) for l in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(x["action"] == "gate1_approved" and x["actor"] == "dhiraj" for x in audit)


def test_gate1_rejection(tmp_path):
    g, conf, _ = _run(tmp_path)
    assert g.invoke(Command(resume={"approved": False, "approver": "lead"}), conf)["status"] == "g1_rejected"


def test_write_capable_login_blocks_the_run(tmp_path):
    def bad(src):
        return SeedConnector(src["id"], {**src, "violations": ["INSERT", "ALTER"]})
    g, conf, out = _run(tmp_path, factory=bad)
    assert out["status"] == "blocked" and g.get_state(conf).next == ()
    assert {i["code"] for i in out["issues"]} == {"SOURCE_NOT_READONLY"}
    assert not (tmp_path / "inventory").exists()                    # nothing was extracted


def test_unreachable_source_blocks(tmp_path):
    def boom(src):
        raise ConnectionError("no route")
    _, _, out = _run(tmp_path, factory=boom)
    assert out["status"] == "blocked" and out["issues"][0]["code"] == "SOURCE_UNREACHABLE"
