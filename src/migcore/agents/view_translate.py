"""View Translation agent: deterministic sqlglot transpile with catalog-driven reference rewriting.

Policy: never emit SQL that is known to change meaning. Constructs whose automatic translation is unsafe
(e.g. Oracle `(+)` outer joins, which sqlglot silently turns into inner joins) are routed to `manual_required`
with NO deployable artifact."""
from __future__ import annotations
import sqlglot
from sqlglot import exp
from migcore.ast.catalog import Catalog

DIALECT = {"sqlserver": "tsql", "oracle": "oracle"}
BLOCKING = {"OUTER_JOIN_PLUS": "Oracle (+) outer-join syntax: generic transpilers drop or mangle it (inner-join result). Rewrite to ANSI LEFT/RIGHT JOIN by hand or add a verified rule.",
            "CONNECT_BY": "hierarchical CONNECT BY needs a recursive CTE redesign",
            "OPENXML": "OPENXML has no Databricks equivalent",
            "FOR_XML": "FOR XML has no Databricks equivalent",
            "ROWNUM": "ROWNUM semantics depend on evaluation order; rewrite with ROW_NUMBER()/LIMIT deliberately"}
REVIEW = {"CROSS_APPLY": "CROSS/OUTER APPLY -> LATERAL: verify join semantics", "PIVOT": "PIVOT/UNPIVOT: verify column set and ordering"}
NOTES = {"NOLOCK_HINT": "Table hints (NOLOCK etc.) were dropped. Databricks reads are snapshot-isolated, so results can differ from SQL Server dirty reads.",
         "TOP_N": "TOP without ORDER BY is non-deterministic in both engines; confirm intended ordering."}


def _split3(target: str) -> tuple[str, str, str]:
    c, s, n = target.split(".")
    return c, s, n


def translate_views(design: dict, source_id: str, engine: str, view_objs: list[dict], irs: dict, cat: Catalog) -> tuple[list[dict], dict[str, str]]:
    dialect, entries, files = DIALECT[engine], [], {}
    for o in view_objs:
        oid = o["fqn"] if "fqn" in o else f"{o['source_id']}.{o['schema_name']}.{o['name']}".lower()
        tgt = design["views"][oid]["target"]
        ir = irs.get(oid, {})
        e = {"object_id": oid, "kind": "VIEW", "target": tgt, "status": "translated", "flags": [], "rules_applied": ["sqlglot_transpile"],
             "artifact": None, "behaviour_changes": [], "blocked_on": []}
        entries.append(e)
        if not o.get("code"):
            e.update(status="manual_required", flags=[{"code": "CODE_UNAVAILABLE", "severity": "high", "detail": "view definition unreadable (encrypted?)"}])
            continue
        blockers = [(k, v) for k, v in BLOCKING.items() if k in ir.get("constructs", [])]
        if blockers:
            e["status"] = "manual_required"
            e["flags"] = [{"code": f"BLOCKED_{k}", "severity": "high", "detail": v} for k, v in blockers]
            continue
        try:
            tree = sqlglot.parse_one(o["code"], read=dialect)
            body = tree.expression
            if not isinstance(tree, exp.Create) or body is None:
                raise ValueError("not a CREATE VIEW ... AS <query>")
        except Exception as ex:
            e.update(status="manual_required", flags=[{"code": "PARSE_FAILED", "severity": "high", "detail": str(ex).splitlines()[0][:200]}])
            continue
        cte_names = {c.alias_or_name.lower() for c in body.find_all(exp.CTE)}
        from_schema = o["schema_name"]
        for tb in list(body.find_all(exp.Table)):
            if not tb.name or tb.name.lower() in cte_names:
                continue
            fqn, _ = cat.resolve_relation(tb.db or None, tb.name, from_schema)
            t = (design["tables"].get(fqn) or design["views"].get(fqn)) if fqn else None
            if not t:
                e["flags"].append({"code": "UNRESOLVED_REFERENCE", "severity": "high", "detail": f"{tb.sql(dialect)} not found in discovered catalog"})
                continue
            c, s, n = _split3(t["target"])
            new = exp.Table(this=exp.to_identifier(n), db=exp.to_identifier(s), catalog=exp.to_identifier(c))
            if tb.args.get("alias"):
                new.set("alias", tb.args["alias"])
            tb.replace(new)
        for d in list(body.find_all(exp.Dot)):
            if isinstance(d.expression, exp.Anonymous):
                fqn = cat.resolve_callable(d.this.name, d.expression.name, from_schema)
                if fqn and fqn in design["callables"]:
                    c, s, n = _split3(design["callables"][fqn]["target"])
                    d.this.replace(exp.Dot(this=exp.to_identifier(c), expression=exp.to_identifier(s)))
                    d.expression.set("this", n)
                    e["blocked_on"].append(fqn)
        for a in list(body.find_all(exp.Anonymous)):
            if isinstance(a.parent, exp.Dot):
                continue
            fqn = cat.resolve_callable(None, a.name, from_schema)
            if fqn and fqn in design["callables"]:
                a.set("this", design["callables"][fqn]["target"].rsplit(".", 1)[1])
                a.replace(exp.Dot(this=exp.Dot(this=exp.to_identifier(_split3(design["callables"][fqn]["target"])[0]),
                                                expression=exp.to_identifier(_split3(design["callables"][fqn]["target"])[1])), expression=a.copy()))
                e["blocked_on"].append(fqn)
        e["blocked_on"] = sorted(set(e["blocked_on"]))
        for k, v in REVIEW.items():
            if k in ir.get("constructs", []):
                e["flags"].append({"code": f"REVIEW_{k}", "severity": "medium", "detail": v})
        for k, v in NOTES.items():
            if k in ir.get("constructs", []):
                e["behaviour_changes"].append(v)
        if e["blocked_on"]:
            e["flags"].append({"code": "BLOCKED_ON_DEPENDENCY", "severity": "medium",
                               "detail": "references functions that are translated in a later milestone: " + ", ".join(e["blocked_on"])})
        if any(f["severity"] == "high" for f in e["flags"]) or e["blocked_on"] or any(f["severity"] == "medium" for f in e["flags"]):
            e["status"] = "needs_review"
        sql = f"CREATE OR REPLACE VIEW {tgt} AS\n{body.sql(dialect='databricks', pretty=True)};\n"
        rel = f"views/{source_id}.{o['schema_name']}.{o['name']}.sql".lower()
        files[rel] = f"-- source: {oid}  hash: {o.get('source_hash') or ''}\n" + sql
        e["artifact"] = rel
    return entries, files
