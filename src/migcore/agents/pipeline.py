"""Agent node implementations for milestone 0: guardrail -> discovery -> ast -> graph -> analyze.
Heavy data lives on disk under runs/<run_id>/ so LangGraph checkpoints stay small."""
from __future__ import annotations
import json
import time
from pathlib import Path
from migcore.ast.catalog import Catalog
from migcore.ast.parser import build_ir
from migcore.graph.payload import build_payload
from migcore.agents.analyzer import analyze, render_markdown
from migcore.models import SourceObject


def audit(run_dir: Path, actor: str, action: str, detail: dict | None = None) -> None:
    with (run_dir / "audit.jsonl").open("a") as f:
        f.write(json.dumps({"ts": time.time(), "actor": actor, "action": action, "detail": detail or {}}) + "\n")


def _w(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2, default=str))


def guardrail(state: dict, cfg: dict, factory) -> dict:
    run_dir = Path(state["run_dir"])
    issues, ok = [], True
    for src in cfg["sources"]:
        try:
            conn = factory(src)
            try:
                violations = conn.check_readonly()
            finally:
                conn.close()
        except Exception as e:  # connectivity problems are blocking too
            issues.append({"code": "SOURCE_UNREACHABLE", "severity": "critical", "object_id": src["id"], "detail": f"{type(e).__name__}: {e}"})
            ok = False
            continue
        if violations:
            ok = False
            issues.append({"code": "SOURCE_NOT_READONLY", "severity": "critical", "object_id": src["id"],
                           "detail": f"write-capable permissions detected: {violations}. Use a read-only login."})
    audit(run_dir, "guardrail", "checked", {"ok": ok, "issues": issues})
    return {"status": "running" if ok else "blocked", "issues": issues}


def discovery(state: dict, cfg: dict, factory) -> dict:
    run_dir = Path(state["run_dir"])
    summary = {}
    for src in cfg["sources"]:
        conn = factory(src)
        try:
            tables, fks, objs = conn.list_tables(), conn.list_foreign_keys(), conn.list_code_objects()
        finally:
            conn.close()
        merged: dict[tuple, SourceObject] = {}
        for o in objs:  # merge Oracle package spec + body into one PACKAGE unit
            if o.kind in ("PACKAGE", "PACKAGE_BODY"):
                key = (o.schema_name, o.name)
                cur = merged.get(key)
                spec = o.code if o.kind == "PACKAGE" else (cur.meta.get("spec") if cur else None)
                body = o.code if o.kind == "PACKAGE_BODY" else (cur.meta.get("body") if cur else None)
                merged[key] = SourceObject(source_id=o.source_id, engine=o.engine, kind="PACKAGE", schema_name=o.schema_name,
                                           name=o.name, code="\n/\n".join(x for x in (spec, body) if x),
                                           meta={"spec": spec, "body": body})
            else:
                merged[(o.schema_name, o.name, o.kind)] = o
        final = list(merged.values())
        for o in final:
            if o.code is not None:
                _w(run_dir / "source" / src["id"] / f"{o.schema_name}.{o.name}.{o.kind.lower()}.sql", o.code)
        inv = {"source_id": src["id"], "engine": src["engine"], "tables": tables, "foreign_keys": fks,
               "objects": [{"id": o.fqn, "kind": o.kind, "schema": o.schema_name, "name": o.name, "source_hash": o.source_hash,
                            "available": o.code is not None, "meta": {k: v for k, v in o.meta.items() if k not in ("spec", "body")}}
                           for o in final]}
        _w(run_dir / "inventory" / f"{src['id']}.json", inv)
        # codes kept separately so IR can be rebuilt from disk
        _w(run_dir / "inventory" / f"{src['id']}.code.json", [o.model_dump() for o in final])
        summary[src["id"]] = {"tables": len(tables), "foreign_keys": len(fks), "code_objects": len(final),
                              "unavailable": [o.fqn for o in final if o.code is None]}
    audit(run_dir, "discovery", "extracted", summary)
    return {"discovery": summary}


def ast_preprocess(state: dict, cfg: dict) -> dict:
    run_dir = Path(state["run_dir"])
    stats, issues = {}, []
    for src in cfg["sources"]:
        inv = json.loads((run_dir / "inventory" / f"{src['id']}.json").read_text())
        objs = [SourceObject(**d) for d in json.loads((run_dir / "inventory" / f"{src['id']}.code.json").read_text())]
        cat = Catalog.from_inventory(src["id"], inv["tables"], objs)
        irs = {}
        for o in objs:
            ir = build_ir(o, cat)
            irs[o.fqn] = ir
            _w(run_dir / "ir" / src["id"] / f"{o.schema_name}.{o.name}.{o.kind.lower()}.json", ir)
            issues += [{**i, "object_id": o.fqn} for i in ir["issues"]]
        _w(run_dir / "ir" / f"{src['id']}.all.json", irs)
        by_status: dict[str, int] = {}
        for ir in irs.values():
            by_status[ir["parse_status"]] = by_status.get(ir["parse_status"], 0) + 1
        stats[src["id"]] = {"objects": len(irs), "parse_status": by_status}
    audit(run_dir, "ast", "parsed", stats)
    return {"ast": stats, "issues": issues}


def graph_build(state: dict, cfg: dict) -> dict:
    run_dir = Path(state["run_dir"])
    payload = {"nodes": [], "edges": []}
    for src in cfg["sources"]:
        inv = json.loads((run_dir / "inventory" / f"{src['id']}.json").read_text())
        irs = json.loads((run_dir / "ir" / f"{src['id']}.all.json").read_text())
        p = build_payload(src["id"], src["engine"], inv["tables"], inv["foreign_keys"], inv["objects"], irs)
        payload["nodes"] += p["nodes"]
        payload["edges"] += p["edges"]
    _w(run_dir / "graph_payload.json", payload)
    result = {"nodes": len(payload["nodes"]), "edges": len(payload["edges"]), "neo4j_loaded": False}
    issues = []
    if cfg.get("graph", {}).get("enabled", True):
        try:
            from migcore.graph.neo4j_loader import load_payload
            load_payload(payload, state["run_id"])
            result["neo4j_loaded"] = True
        except Exception as e:  # analysis continues from the payload; flag it
            issues.append({"code": "NEO4J_LOAD_FAILED", "severity": "medium", "detail": f"{type(e).__name__}: {e}"})
    audit(run_dir, "graph", "built", result)
    return {"graph": result, "issues": issues}


def analyze_node(state: dict, cfg: dict) -> dict:
    run_dir = Path(state["run_dir"])
    payload = json.loads((run_dir / "graph_payload.json").read_text())
    irs = {}
    for src in cfg["sources"]:
        irs.update(json.loads((run_dir / "ir" / f"{src['id']}.all.json").read_text()))
    a = analyze(payload, irs)
    _w(run_dir / "assessment.json", a)
    _w(run_dir / "assessment.md", render_markdown(a, state["run_id"]))
    audit(run_dir, "analyzer", "assessed", {"histogram": a["complexity_histogram"], "waves": len(a["waves"])})
    return {"assessment": {"counts": a["counts"], "histogram": a["complexity_histogram"], "waves": len(a["waves"]),
                           "needs_human": len(a["needs_human"]), "cycles": len(a["cycles"])}}
