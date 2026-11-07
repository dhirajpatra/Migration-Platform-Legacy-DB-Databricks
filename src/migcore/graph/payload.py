"""Build the Neo4j node/edge payload from inventory + IR. Pure functions: testable without a database."""
from __future__ import annotations

LABEL = {"TABLE": "Table", "VIEW": "View", "PROCEDURE": "Procedure", "FUNCTION": "Function",
         "PACKAGE": "Package", "TRIGGER": "Trigger"}


def build_payload(source_id: str, engine: str, tables: list[dict], fks: list[dict],
                  objects: list[dict], irs: dict[str, dict]) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def fq(schema: str, name: str) -> str:
        return f"{source_id}.{schema}.{name}".lower()

    for t in tables:
        nodes[fq(t["schema"], t["name"])] = {"id": fq(t["schema"], t["name"]), "label": "Table", "source_id": source_id,
                                            "engine": engine, "schema": t["schema"], "name": t["name"],
                                            "row_estimate": t.get("row_estimate"), "columns": len(t["columns"])}
    for o in objects:
        nodes[o["id"]] = {"id": o["id"], "label": LABEL[o["kind"]], "source_id": source_id, "engine": engine,
                          "schema": o["schema"], "name": o["name"], "source_hash": o["source_hash"]}
    for fk in fks:
        a, b = fq(fk["schema"], fk["table"]), fq(fk["ref_schema"], fk["ref_table"])
        if a in nodes and b in nodes:
            edges.append({"src": a, "dst": b, "type": "FK_TO", "props": {"name": fk["name"]}})
    for o in objects:
        ir = irs.get(o["id"])
        if not ir:
            continue
        for dst in ir["tables_read"]:
            if dst in nodes:
                edges.append({"src": o["id"], "dst": dst, "type": "READS", "props": {}})
        for w in ir["tables_written"]:
            if w["name"] in nodes:
                edges.append({"src": o["id"], "dst": w["name"], "type": "WRITES", "props": {"ops": w["ops"]}})
        for dst in ir["calls"]:
            if dst in nodes:
                edges.append({"src": o["id"], "dst": dst, "type": "CALLS", "props": {"resolution": "name_match"}})
        if o["kind"] == "TRIGGER":
            m = o.get("meta", {})
            dst = fq(m.get("on_schema", ""), m.get("on_table", ""))
            if dst in nodes:
                edges.append({"src": o["id"], "dst": dst, "type": "FIRES_ON",
                              "props": {"events": m.get("events", []), "timing": m.get("timing", "")}})
    return {"nodes": list(nodes.values()), "edges": edges}
