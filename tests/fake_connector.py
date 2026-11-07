"""Connector backed by the seed SQL files: lets the full pipeline run with no database/Docker."""
from __future__ import annotations
import re
from pathlib import Path
from migcore.connectors.base import SourceConnector
from migcore.models import SourceObject

SEED = Path(__file__).resolve().parents[1] / "seed"


def _cols(*names):
    return [{"name": n, "type": "x", "length": None, "precision": None, "scale": None, "nullable": True, "identity": False} for n in names]


class SeedConnector(SourceConnector):
    def __init__(self, source_id: str, cfg: dict):
        super().__init__(source_id, cfg)
        self.engine = cfg["engine"]
        self.violations = cfg.get("violations", [])

    def check_readonly(self):
        return self.violations

    def list_tables(self):
        if self.engine == "sqlserver":
            return [{"schema": "dbo", "name": n, "columns": _cols("a", "b"), "pk": ["a"], "row_estimate": 10}
                    for n in ("Customers", "Orders", "OrderItems", "Ledger", "AuditLog")]
        return [{"schema": "DEMO", "name": n, "columns": _cols("a", "b"), "pk": ["a"], "row_estimate": 10}
                for n in ("CUSTOMERS", "ORDERS", "ORDER_ITEMS", "LEDGER", "AUDIT_LOG")]

    def list_foreign_keys(self):
        if self.engine == "sqlserver":
            return [{"name": "fk1", "schema": "dbo", "table": "Orders", "ref_schema": "dbo", "ref_table": "Customers"},
                    {"name": "fk2", "schema": "dbo", "table": "OrderItems", "ref_schema": "dbo", "ref_table": "Orders"}]
        return [{"name": "fk1", "schema": "DEMO", "table": "ORDERS", "ref_schema": "DEMO", "ref_table": "CUSTOMERS"}]

    def list_code_objects(self):
        out = []
        if self.engine == "sqlserver":
            sql = (SEED / "sqlserver" / "init.sql").read_text()
            for chunk in re.split(r"^\s*GO\s*$", sql, flags=re.M | re.I):
                m = re.search(r"CREATE\s+(FUNCTION|VIEW|PROCEDURE|TRIGGER)\s+(\w+)\.(\w+)", chunk, re.I)
                if not m:
                    continue
                kind = {"FUNCTION": "FUNCTION", "VIEW": "VIEW", "PROCEDURE": "PROCEDURE", "TRIGGER": "TRIGGER"}[m.group(1).upper()]
                code = chunk[m.start():].strip()
                meta = {"on_schema": "dbo", "on_table": "Orders", "events": ["INSERT", "UPDATE"], "timing": "AFTER"} if kind == "TRIGGER" else {}
                out.append(SourceObject(source_id=self.source_id, engine="sqlserver", kind=kind, schema_name=m.group(2), name=m.group(3), code=code, meta=meta))
        else:
            sql = (SEED / "oracle" / "01_demo.sql").read_text()
            for chunk in re.split(r"^/\s*$", sql, flags=re.M):
                m = re.search(r"CREATE OR REPLACE (FUNCTION|VIEW|PACKAGE BODY|PACKAGE|TRIGGER)\s+(\w+)\.(\w+)", chunk, re.I)
                if not m:
                    continue
                kind = m.group(1).upper().replace(" ", "_")
                meta = {"on_schema": "DEMO", "on_table": "ORDERS", "events": ["INSERT", "UPDATE"], "timing": "AFTER EACH ROW"} if kind == "TRIGGER" else {}
                out.append(SourceObject(source_id=self.source_id, engine="oracle", kind=kind, schema_name=m.group(2).upper(), name=m.group(3).upper(), code=chunk[m.start():].strip(), meta=meta))
        return out
