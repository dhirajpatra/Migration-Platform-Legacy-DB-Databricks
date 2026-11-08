from __future__ import annotations
import logging
import os
import threading
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from langgraph.types import Command
from pydantic import BaseModel
from migcore.config import load_config, runs_dir
from migcore.connectors.registry import build_connector
from migcore.orchestrator.graph import build_graph

log = logging.getLogger("migcore.api")


class Approval(BaseModel):
    approved: bool
    approver: str
    comment: str = ""


def _make_checkpointer():
    url = os.environ.get("POSTGRES_URL")
    if url:
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            from psycopg_pool import ConnectionPool
            pool = ConnectionPool(url, max_size=5, kwargs={"autocommit": True, "prepare_threshold": 0}, open=True)
            saver = PostgresSaver(pool)
            saver.setup()
            log.info("Using Postgres checkpointer")
            return saver
        except Exception as e:
            log.warning("Postgres checkpointer unavailable (%s); falling back to in-memory (runs will NOT survive restart)", e)
    from langgraph.checkpoint.memory import MemorySaver
    return MemorySaver()


def create_app(cfg: dict | None = None, connector_factory=build_connector, checkpointer=None) -> FastAPI:
    app = FastAPI(title="Migration Orchestrator", version="0.1.0")
    state = {"graph": None, "cfg": cfg, "errors": {}}

    def graph():
        if state["graph"] is None:
            state["cfg"] = state["cfg"] or load_config()
            state["graph"] = build_graph(state["cfg"], connector_factory, checkpointer or _make_checkpointer())
        return state["graph"]

    def _bg(run_id: str, fn) -> None:
        def target():
            try:
                fn()
            except Exception as e:  # surfaced via GET /runs/{id}
                log.exception("run %s failed", run_id)
                state["errors"][run_id] = f"{type(e).__name__}: {e}"
        threading.Thread(target=target, daemon=True).start()

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/runs", status_code=202)
    def start_run():
        run_id = uuid.uuid4().hex[:12]
        run_dir = runs_dir() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        g = graph()
        _bg(run_id, lambda: g.invoke({"run_id": run_id, "run_dir": str(run_dir), "status": "running", "issues": []},
                                      {"configurable": {"thread_id": run_id}}))
        return {"run_id": run_id}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        snap = graph().get_state({"configurable": {"thread_id": run_id}})
        if not snap.values and run_id not in state["errors"]:
            raise HTTPException(404, "unknown run (or still starting)")
        v = dict(snap.values or {})
        nxt = list(snap.next or ())
        gate = next((g for g in ("gate1", "gate2") if g in nxt), None)
        return {"run_id": run_id, "status": f"awaiting_g{gate[-1]}" if gate else v.get("status"), "next": nxt,
                "error": state["errors"].get(run_id), "discovery": v.get("discovery"), "ast": v.get("ast"),
                "graph": v.get("graph"), "assessment": v.get("assessment"), "design": v.get("design"),
                "translation": v.get("translation"), "verification": v.get("verification"), "issues": v.get("issues", [])}

    @app.get("/runs/{run_id}/assessment", response_class=PlainTextResponse)
    def assessment(run_id: str):
        f = runs_dir() / run_id / "assessment.md"
        if not f.exists():
            raise HTTPException(404, "assessment not ready")
        return f.read_text()

    @app.get("/runs/{run_id}/report/{name}", response_class=PlainTextResponse)
    def report(run_id: str, name: str):
        """Allow-listed run artifacts: assessment.md, design.json, translation_report.md"""
        allowed = {"assessment.md": "assessment.md", "design.json": "target/design.json",
                   "translation_report.md": "target/translation_report.md"}
        if name not in allowed:
            raise HTTPException(404, f"unknown report; choose one of {sorted(allowed)}")
        f = runs_dir() / run_id / allowed[name]
        if not f.exists():
            raise HTTPException(404, "not ready yet")
        return f.read_text()

    @app.get("/target/status")
    def target_status():
        """Which cloud module is active and whether Databricks is mock or real."""
        from migcore.cloud import get_provider
        from migcore.cloud.databricks import DatabricksClient
        cfg_ = state["cfg"] or load_config()
        state["cfg"] = cfg_
        prov, db = get_provider(cfg_), DatabricksClient.from_env(cfg_)
        return {"cloud": prov.name, "databricks": db.whoami(), "tooling": prov.tooling, "unity_catalog": prov.uc_requirements()}

    @app.post("/runs/{run_id}/cloud-plan")
    def cloud_plan(run_id: str, cloud: str | None = None, mode: str = "full_load_and_cdc"):
        """Generate the cloud-specific load plan + artifacts (DMS for AWS, ADF for Azure) from this run's inventory."""
        from migcore.cloud.__main__ import generate
        state["cfg"] = state["cfg"] or load_config()
        try:
            return generate(run_id, state["cfg"], cloud, mode)
        except FileNotFoundError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/runs/{run_id}/approve")
    def approve(run_id: str, body: Approval):
        g = graph()
        snap = g.get_state({"configurable": {"thread_id": run_id}})
        if not any(g in (snap.next or ()) for g in ("gate1", "gate2")):
            raise HTTPException(409, "run is not waiting at a gate")
        _bg(run_id, lambda: g.invoke(Command(resume=body.model_dump()), {"configurable": {"thread_id": run_id}}))
        return {"accepted": True}

    return app


app = create_app()
