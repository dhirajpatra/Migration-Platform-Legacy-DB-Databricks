from __future__ import annotations
import os
from collections import defaultdict


def load_payload(payload: dict, run_id: str) -> dict:
    """Idempotent upsert (MERGE on stable id). Raises if Neo4j is unreachable."""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"]))
    try:
        with driver.session() as s:
            s.run("CREATE CONSTRAINT dbobject_id IF NOT EXISTS FOR (n:DbObject) REQUIRE n.id IS UNIQUE").consume()
            by_label: dict[str, list[dict]] = defaultdict(list)
            for n in payload["nodes"]:
                props = {k: v for k, v in n.items() if k != "label" and v is not None}
                by_label[n["label"]].append(props)
            for label, rows in by_label.items():  # label comes from a fixed allow-list in payload.LABEL
                s.run(f"UNWIND $rows AS r MERGE (n:DbObject {{id: r.id}}) SET n += r, n:{label}, n.last_run = $run",
                      rows=rows, run=run_id).consume()
            by_type: dict[str, list[dict]] = defaultdict(list)
            for e in payload["edges"]:
                by_type[e["type"]].append({"src": e["src"], "dst": e["dst"], "props": e["props"]})
            for rtype, rows in by_type.items():
                s.run(f"UNWIND $rows AS r MATCH (a:DbObject {{id: r.src}}) MATCH (b:DbObject {{id: r.dst}}) "
                      f"MERGE (a)-[x:{rtype}]->(b) SET x += r.props", rows=rows).consume()
        return {"nodes": len(payload["nodes"]), "edges": len(payload["edges"])}
    finally:
        driver.close()
