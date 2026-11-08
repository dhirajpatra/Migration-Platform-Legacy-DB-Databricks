"""Connector backed by the seed SQL files: lets the full pipeline run with no database/Docker."""
from __future__ import annotations
import re
from pathlib import Path
from migcore.connectors.base import SourceConnector
from migcore.models import SourceObject

SEED = Path(__file__).resolve().parents[1] / "seed"


def _c(name, typ, nullable=True, identity=False, precision=None, scale=None, length=None):
    return {"name": name, "type": typ, "length": length, "precision": precision, "scale": scale, "nullable": nullable, "identity": identity}


class SeedConnector(SourceConnector):
    def __init__(self, source_id: str, cfg: dict):
        super().__init__(source_id, cfg)
        self.engine = cfg["engine"]
        self.violations = cfg.get("violations", [])

    def check_readonly(self):
        return self.violations

    def list_tables(self):
        if self.engine == "sqlserver":
            return [
                {"schema": "dbo", "name": "Customers", "pk": ["CustomerId"], "row_estimate": 2_500_000, "columns": [
                    _c("CustomerId", "int", False, True), _c("Name", "nvarchar", False, length=200), _c("Email", "varchar", length=200),
                    _c("CreditLimit", "money", False), _c("CreatedAt", "datetime", False), _c("LastOrder", "datetime")]},
                {"schema": "dbo", "name": "Orders", "pk": ["OrderId"], "row_estimate": 10, "columns": [
                    _c("OrderId", "int", False, True), _c("CustomerId", "int", False), _c("OrderDate", "datetime", False),
                    _c("Status", "varchar", False, length=20), _c("RowGuid", "uniqueidentifier", False)]},
                {"schema": "dbo", "name": "OrderItems", "pk": ["OrderItemId"], "row_estimate": 10, "columns": [
                    _c("OrderItemId", "int", False, True), _c("OrderId", "int", False), _c("Sku", "varchar", False, length=40),
                    _c("Qty", "int", False), _c("UnitPrice", "decimal", False, precision=12, scale=2)]},
                {"schema": "dbo", "name": "Ledger", "pk": ["LedgerId"], "row_estimate": 10, "columns": [
                    _c("LedgerId", "int", False, True), _c("CustomerId", "int", False), _c("Period", "date", False), _c("Amount", "decimal", False, precision=18, scale=2)]},
                {"schema": "dbo", "name": "AuditLog", "pk": ["AuditId"], "row_estimate": 10, "columns": [
                    _c("AuditId", "int", False, True), _c("TableName", "sysname", False), _c("Action", "varchar", False, length=10),
                    _c("KeyValue", "int"), _c("At", "datetime", False)]}]
        return [
            {"schema": "DEMO", "name": "CUSTOMERS", "pk": ["CUSTOMER_ID"], "row_estimate": 100, "columns": [
                _c("CUSTOMER_ID", "NUMBER", False, precision=10, scale=0), _c("NAME", "VARCHAR2", False, length=100), _c("EMAIL", "VARCHAR2", length=200),
                _c("CREDIT_LIMIT", "NUMBER", False, precision=12, scale=2), _c("CREATED_AT", "DATE", False)]},
            {"schema": "DEMO", "name": "ORDERS", "pk": ["ORDER_ID"], "row_estimate": 100, "columns": [
                _c("ORDER_ID", "NUMBER", False, precision=10, scale=0), _c("CUSTOMER_ID", "NUMBER", False, precision=10, scale=0),
                _c("ORDER_DATE", "DATE", False), _c("STATUS", "VARCHAR2", False, length=20)]},
            {"schema": "DEMO", "name": "ORDER_ITEMS", "pk": ["ITEM_ID"], "row_estimate": 100, "columns": [
                _c("ITEM_ID", "NUMBER", False, precision=10, scale=0), _c("ORDER_ID", "NUMBER", False, precision=10, scale=0),
                _c("SKU", "VARCHAR2", False, length=40), _c("QTY", "NUMBER", False, precision=6, scale=0), _c("UNIT_PRICE", "NUMBER", False, precision=12, scale=2)]},
            {"schema": "DEMO", "name": "LEDGER", "pk": ["LEDGER_ID"], "row_estimate": 100, "columns": [
                _c("LEDGER_ID", "NUMBER", False, precision=10, scale=0), _c("CUSTOMER_ID", "NUMBER", False, precision=10, scale=0),
                _c("PERIOD", "DATE", False), _c("AMOUNT", "NUMBER", False)]},
            {"schema": "DEMO", "name": "AUDIT_LOG", "pk": ["AUDIT_ID"], "row_estimate": 100, "columns": [
                _c("AUDIT_ID", "NUMBER", False, precision=10, scale=0), _c("TABLE_NAME", "VARCHAR2", length=30), _c("ACTION", "VARCHAR2", length=10),
                _c("KEY_VALUE", "NUMBER"), _c("AT", "TIMESTAMP(6)")]}]

    def list_foreign_keys(self):
        if self.engine == "sqlserver":
            return [{"name": "fk_orders_cust", "schema": "dbo", "table": "Orders", "ref_schema": "dbo", "ref_table": "Customers", "columns": ["CustomerId"], "ref_columns": ["CustomerId"]},
                    {"name": "fk_items_order", "schema": "dbo", "table": "OrderItems", "ref_schema": "dbo", "ref_table": "Orders", "columns": ["OrderId"], "ref_columns": ["OrderId"]}]
        return [{"name": "FK_ORDERS_CUST", "schema": "DEMO", "table": "ORDERS", "ref_schema": "DEMO", "ref_table": "CUSTOMERS", "columns": ["CUSTOMER_ID"], "ref_columns": ["CUSTOMER_ID"]}]

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
