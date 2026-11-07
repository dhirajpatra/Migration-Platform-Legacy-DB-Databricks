"""Analyzer/Planner: complexity scoring, dependency waves, SCCs, hotspots, dead-code candidates."""
from __future__ import annotations
import networkx as nx

HARD_CONSTRUCTS = {"CONNECT_BY", "BULK_COLLECT", "FORALL", "AUTONOMOUS_TRANSACTION", "OPENXML", "FOR_XML", "PIVOT",
                   "MERGE", "OUTPUT_CLAUSE", "CROSS_APPLY", "TEMP_TABLE", "TABLE_VARIABLE", "GOTO", "CURSOR",
                   "OUTER_JOIN_PLUS", "FOR_UPDATE", "SEQUENCE_NEXTVAL", "INSERTED_DELETED", "NEW_OLD_REFS"}
FORCE_REVIEW = ("DYN_SQL_UNRESOLVED", "AUTONOMOUS_TXN", "CODE_UNAVAILABLE", "PROMPT_INJECTION_SUSPECTED")
BUCKETS = [(25, "Simple"), (55, "Moderate"), (80, "Complex"), (101, "Redesign")]
WEIGHTS = {"size": 10, "flow": 15, "constructs": 20, "dynamic": 15, "side": 15, "txn": 10, "central": 10, "tests": 5}


def score_object(ir: dict, fan_in: int) -> dict:
    cf = ir.get("control_flow", {})
    s = {}
    s["size"] = min(ir.get("loc", 0) / 400, 1) * WEIGHTS["size"]
    s["flow"] = min((cf.get("cursors", 0) * 4 + cf.get("loops", 0) * 3 + cf.get("branches", 0) * 0.5
                     + (2 if cf.get("try_catch") else 0)) / 15, 1) * WEIGHTS["flow"]
    s["constructs"] = min(len(HARD_CONSTRUCTS & set(ir.get("constructs", []))) * 4, WEIGHTS["constructs"])
    dyn = ir.get("dynamic_sql", [])
    s["dynamic"] = WEIGHTS["dynamic"] if any(not d["resolved"] for d in dyn) else (5 if dyn else 0)
    s["side"] = WEIGHTS["side"] if ir.get("side_effects") else 0
    s["txn"] = min(cf.get("transactions", 0) * 3, WEIGHTS["txn"])
    s["central"] = min(fan_in / 10, 1) * WEIGHTS["central"]
    s["tests"] = WEIGHTS["tests"]  # no known behavioural tests in v0 => penalty
    total = round(min(sum(s.values()), 100), 1)
    codes = {i["code"] for i in ir.get("issues", [])}
    hard = sorted(c for c in codes if c in FORCE_REVIEW or c.startswith("SIDE_EFFECT_"))
    if ir.get("cross_db_refs"):
        hard.append("CROSS_DB_REF")
    bucket = next(b for lim, b in BUCKETS if total < lim)
    if hard and bucket in ("Simple", "Moderate"):
        bucket = "Complex"
    return {"score": total, "bucket": bucket, "factors": {k: round(v, 1) for k, v in s.items()},
            "hard_flags": hard, "needs_human": bool(hard) or bucket in ("Complex", "Redesign")}


def analyze(payload: dict, irs: dict[str, dict]) -> dict:
    nodes = {n["id"]: n for n in payload["nodes"]}
    dep = nx.DiGraph()  # edge A -> B  means "A depends on B"
    dep.add_nodes_from(nodes)
    for e in payload["edges"]:
        if e["type"] in ("READS", "WRITES", "CALLS", "FK_TO", "FIRES_ON"):
            dep.add_edge(e["src"], e["dst"])
    fan_in = {n: dep.in_degree(n) for n in dep}

    scores = {oid: score_object(ir, fan_in.get(oid, 0)) for oid, ir in irs.items()}

    cond = nx.condensation(dep)  # SCCs collapsed -> DAG
    wave_of: dict[int, int] = {}
    for c in reversed(list(nx.topological_sort(cond))):  # dependencies first
        deps = list(cond.successors(c))
        wave_of[c] = 0 if not deps else 1 + max(wave_of[d] for d in deps)
    waves: dict[int, list[str]] = {}
    for c, w in wave_of.items():
        waves.setdefault(w, []).extend(sorted(cond.nodes[c]["members"]))
    sccs = [sorted(m) for m in (cond.nodes[c]["members"] for c in cond.nodes) if len(m) > 1]

    code_ids = {n for n, v in nodes.items() if v["label"] != "Table"}
    dead = sorted(n for n in code_ids if fan_in.get(n, 0) == 0 and nodes[n]["label"] in ("Procedure", "Function", "Package", "View"))
    hist: dict[str, int] = {}
    for sc in scores.values():
        hist[sc["bucket"]] = hist.get(sc["bucket"], 0) + 1
    kinds: dict[str, int] = {}
    for n in nodes.values():
        kinds[n["label"]] = kinds.get(n["label"], 0) + 1
    hotspots = sorted(((fan_in[n] * (1 + scores.get(n, {"score": 0})["score"] / 100), n) for n in code_ids),
                      reverse=True)[:5]
    return {
        "counts": kinds, "complexity_histogram": hist, "scores": scores,
        "waves": [{"wave": w, "objects": waves[w]} for w in sorted(waves)],
        "cycles": sccs,
        "needs_human": sorted(o for o, s in scores.items() if s["needs_human"]),
        "dead_code_candidates": dead,   # NEVER auto-dropped: needs usage telemetry + human confirmation
        "hotspots": [n for _, n in hotspots],
        "edges": len(payload["edges"]),
    }


def render_markdown(a: dict, run_id: str) -> str:
    L = [f"# Assessment Report — run `{run_id}`", "", "## Inventory"]
    L += [f"- **{k}**: {v}" for k, v in sorted(a["counts"].items())]
    L += ["", "## Complexity (code objects)"] + [f"- {k}: {v}" for k, v in sorted(a["complexity_histogram"].items())]
    L += ["", "## Wave plan (dependencies first)"]
    for w in a["waves"]:
        L.append(f"- **Wave {w['wave']}** ({len(w['objects'])}): " + ", ".join(o.split('.', 1)[1] for o in w["objects"]))
    L += ["", "## Needs human review"]
    L += [f"- `{o}` — {a['scores'][o]['bucket']} ({a['scores'][o]['score']}); flags: {', '.join(a['scores'][o]['hard_flags']) or '—'}"
          for o in a["needs_human"]] or ["- none"]
    L += ["", "## Dependency cycles"] + ([f"- {' ⇄ '.join(c)}" for c in a["cycles"]] or ["- none"])
    L += ["", "## Dead-code candidates (confirm with usage telemetry; never auto-removed)"]
    L += [f"- `{o}`" for o in a["dead_code_candidates"]] or ["- none"]
    L += ["", "## Hotspots (fan-in × complexity)"] + [f"- `{o}`" for o in a["hotspots"]]
    return "\n".join(L) + "\n"
