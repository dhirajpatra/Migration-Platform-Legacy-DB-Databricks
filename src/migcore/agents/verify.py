"""Verifier (non-LLM). Mock Databricks: SYNTAX only -> `syntax_verified`. Real warehouse: executes into a SANDBOX
catalog (compiles views for real) -> `compile_verified`. `auto_verified` is reserved for objects that also pass
data-parity / behavioural tests (Milestone 3) - nothing is promoted to it here."""
from __future__ import annotations
from pathlib import Path
import sqlglot


def _stmts(sql: str) -> list[str]:
    return [s.sql("databricks") for s in sqlglot.parse(sql, read="databricks") if s is not None]


def verify(db, design: dict, entries: list[dict], tdir: Path) -> dict:
    cat, sandbox = design["catalog"], design["sandbox_catalog"]
    out = {"mode": db.mode, "checked": 0, "failed": 0, "executed_in_sandbox": 0}

    def sbx(sql: str) -> str:
        return sql.replace(f"{cat}.", f"{sandbox}.")

    if not db.mock:
        db.execute(f"CREATE CATALOG IF NOT EXISTS {sandbox}")
        for s in sorted({t["target"].rsplit(".", 1)[0] for t in list(design["tables"].values()) + list(design["views"].values())}):
            db.execute(f"CREATE SCHEMA IF NOT EXISTS {sbx(s)}")

    def run(e: dict, sql: str) -> None:
        out["checked"] += 1
        syn = db.check_syntax(sql)
        e["syntax_ok"] = syn["ok"]
        if not syn["ok"]:
            e["status"], e["verify_error"] = "failed", syn["error"]
            out["failed"] += 1
            return
        if db.mock:
            if e["status"] == "translated":
                e["status"] = "syntax_verified"
            return
        try:
            for st in _stmts(sbx(sql)):
                r = db.execute(st)
                if r["status"] != "SUCCEEDED":
                    raise RuntimeError(str(r.get("error")))
            out["executed_in_sandbox"] += 1
            e["compiled"] = True
            if e["status"] == "translated":
                e["status"] = "compile_verified"
        except Exception as ex:
            e["compiled"], e["compile_error"] = False, str(ex)[:300]
            if e["status"] == "translated":
                e["status"] = "failed"
                out["failed"] += 1

    for kind in ("TABLE", "VIEW"):                       # tables first so views can compile in real mode
        for e in (x for x in entries if x["kind"] == kind and x.get("artifact")):
            run(e, (tdir / e["artifact"]).read_text())
        if kind == "TABLE":
            cons = tdir / "ddl" / "constraints.sql"
            if cons.exists():
                fake = {"status": "translated", "kind": "CONSTRAINTS"}
                run(fake, cons.read_text())
                out["constraints"] = fake["status"]
    return out
