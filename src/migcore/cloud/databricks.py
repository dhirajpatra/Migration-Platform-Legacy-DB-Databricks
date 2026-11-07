"""Databricks target client. MOCK by default (placeholder token) so the platform runs end-to-end
without a workspace; flips to REAL automatically when a real host + token are provided.

Mock mode: no network. `execute` records the statement and succeeds; `explain`/`check_syntax` use sqlglot's
Databricks dialect so malformed generated SQL still fails fast in dev.
Real mode: Databricks SQL Statement Execution API (`/api/2.0/sql/statements`) against a SQL warehouse."""
from __future__ import annotations
import os
import time
import sqlglot

MOCK_TOKEN = "dapi-mock-0000"


class DatabricksClient:
    def __init__(self, host: str = "", token: str = "", warehouse_id: str = "", catalog: str = "migration_dev"):
        self.host = (host or "").rstrip("/")
        self.token = token or ""
        self.warehouse_id = warehouse_id
        self.catalog = catalog
        self.mock = (not self.host) or (not self.token) or self.token.lower().startswith(("mock", "dapi-mock"))
        self.calls: list[dict] = []   # audit of every statement issued (mock and real)

    @classmethod
    def from_env(cls, cfg: dict | None = None) -> "DatabricksClient":
        t = ((cfg or {}).get("engagement", {}) or {}).get("target", {}) or {}
        return cls(host=os.environ.get("DATABRICKS_HOST") or t.get("host", ""),
                   token=os.environ.get("DATABRICKS_TOKEN", MOCK_TOKEN),
                   warehouse_id=os.environ.get("DATABRICKS_WAREHOUSE_ID") or t.get("warehouse_id", ""),
                   catalog=t.get("catalog", "migration_dev"))

    @property
    def mode(self) -> str:
        return "mock" if self.mock else "real"

    # ------------------------------------------------------------------ REST helper
    def _http(self, method: str, path: str, **kw) -> dict:
        import httpx
        r = httpx.request(method, f"{self.host}{path}", headers={"Authorization": f"Bearer {self.token}"}, timeout=60, **kw)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ API
    def whoami(self) -> dict:
        if self.mock:
            return {"mode": "mock", "user": "mock-user@example.com", "host": self.host or "(none)"}
        me = self._http("GET", "/api/2.0/preview/scim/v2/Me")
        return {"mode": "real", "user": me.get("userName"), "host": self.host}

    def check_syntax(self, sql: str) -> dict:
        """Static check with sqlglot (Databricks dialect). Works in both modes; no warehouse needed."""
        try:
            for stmt in sqlglot.parse(sql, read="databricks"):
                if stmt is None:
                    continue
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e).splitlines()[0][:300]}

    def execute(self, sql: str) -> dict:
        rec = {"ts": time.time(), "mode": self.mode, "statement": sql}
        self.calls.append(rec)
        if self.mock:
            return {"mode": "mock", "status": "SUCCEEDED", "rows": []}
        if not self.warehouse_id:
            raise RuntimeError("DATABRICKS_WAREHOUSE_ID is required in real mode")
        out = self._http("POST", "/api/2.0/sql/statements",
                         json={"statement": sql, "warehouse_id": self.warehouse_id, "wait_timeout": "30s"})
        return {"mode": "real", "status": out.get("status", {}).get("state"), "rows": out.get("result", {}).get("data_array", []),
                "error": out.get("status", {}).get("error")}

    def explain(self, sql: str) -> dict:
        """Verifier hook (SKILL §8.5). Mock mode can only prove syntax (`compiled` is None = not proven)."""
        syn = self.check_syntax(sql)
        if not syn["ok"]:
            return {**syn, "mode": self.mode, "compiled": False}
        if self.mock:
            return {"ok": True, "mode": "mock", "compiled": None}
        res = self.execute("EXPLAIN " + sql)
        return {"ok": res["status"] == "SUCCEEDED", "mode": "real", "compiled": res["status"] == "SUCCEEDED", "error": res.get("error")}
