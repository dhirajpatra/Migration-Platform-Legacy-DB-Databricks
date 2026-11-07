from __future__ import annotations
from migcore.connectors.base import SourceConnector
from migcore.models import SourceObject

# Permissions meaning the login can change something. A safe migration login has none of these.
WRITE_PERMS = {"INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL", "EXECUTE", "CREATE TABLE",
               "CREATE PROCEDURE", "CREATE VIEW", "CREATE FUNCTION", "TAKE OWNERSHIP",
               "ALTER ANY USER", "ALTER ANY ROLE"}
TYPE_KIND = {"V": "VIEW", "P": "PROCEDURE", "FN": "FUNCTION", "IF": "FUNCTION", "TF": "FUNCTION"}


class SqlServerConnector(SourceConnector):
    engine = "sqlserver"

    def __init__(self, source_id: str, cfg: dict):
        super().__init__(source_id, cfg)
        import pymssql  # lazy: unit tests don't need the driver
        self.conn = pymssql.connect(
            server=cfg["host"], port=str(cfg.get("port", 1433)), user=cfg["user"],
            password=cfg["password"], database=cfg["database"], login_timeout=15, as_dict=True)

    def _q(self, sql: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        cur.close()
        return rows

    def check_readonly(self) -> list[str]:
        perms = {r["permission_name"].upper() for r in self._q(
            "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')")}
        found = sorted(perms & WRITE_PERMS)
        if self._q("SELECT IS_SRVROLEMEMBER('sysadmin') AS v")[0]["v"] == 1:
            found.append("sysadmin")
        return found

    def list_tables(self) -> list[dict]:
        cols = self._q("""
            SELECT s.name AS sch, t.name AS tbl, c.column_id, c.name AS col, ty.name AS dtype,
                   c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.columns c ON c.object_id = t.object_id
            JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE t.is_ms_shipped = 0 ORDER BY s.name, t.name, c.column_id""")
        pks = self._q("""
            SELECT s.name AS sch, t.name AS tbl, c.name AS col, ic.key_ordinal
            FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE i.is_primary_key = 1 ORDER BY s.name, t.name, ic.key_ordinal""")
        rows = self._q("""
            SELECT s.name AS sch, t.name AS tbl, SUM(p.rows) AS n
            FROM sys.partitions p JOIN sys.tables t ON t.object_id = p.object_id
            JOIN sys.schemas s ON s.schema_id = t.schema_id
            WHERE p.index_id IN (0, 1) GROUP BY s.name, t.name""")
        tables: dict[tuple, dict] = {}
        for r in cols:
            t = tables.setdefault((r["sch"], r["tbl"]), {"schema": r["sch"], "name": r["tbl"],
                                                         "columns": [], "pk": [], "row_estimate": None})
            t["columns"].append({"name": r["col"], "type": r["dtype"], "length": r["max_length"],
                                 "precision": r["precision"], "scale": r["scale"],
                                 "nullable": bool(r["is_nullable"]), "identity": bool(r["is_identity"])})
        for r in pks:
            tables[(r["sch"], r["tbl"])]["pk"].append(r["col"])
        for r in rows:
            if (r["sch"], r["tbl"]) in tables:
                tables[(r["sch"], r["tbl"])]["row_estimate"] = int(r["n"] or 0)
        return list(tables.values())

    def list_foreign_keys(self) -> list[dict]:
        return [{"name": r["name"], "schema": r["ps"], "table": r["pt"],
                 "ref_schema": r["rs"], "ref_table": r["rt"]} for r in self._q("""
            SELECT fk.name, ps.name AS ps, pt.name AS pt, rs.name AS rs, rt.name AS rt
            FROM sys.foreign_keys fk
            JOIN sys.tables pt ON pt.object_id = fk.parent_object_id
            JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
            JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
            JOIN sys.schemas rs ON rs.schema_id = rt.schema_id""")]

    def list_code_objects(self) -> list[SourceObject]:
        out: list[SourceObject] = []
        for r in self._q("""
            SELECT s.name AS sch, o.name AS nm, o.type AS tcode, m.definition AS code
            FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
            LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
            WHERE o.is_ms_shipped = 0 AND o.type IN ('V','P','FN','IF','TF')
            ORDER BY s.name, o.name"""):
            out.append(SourceObject(source_id=self.source_id, engine=self.engine,
                                    kind=TYPE_KIND[r["tcode"].strip()], schema_name=r["sch"],
                                    name=r["nm"], code=r["code"], meta={"type_code": r["tcode"].strip()}))
        for r in self._q("""
            SELECT OBJECT_SCHEMA_NAME(tr.object_id) AS sch, tr.name AS nm,
                   OBJECT_SCHEMA_NAME(tr.parent_id) AS psch, OBJECT_NAME(tr.parent_id) AS ptbl,
                   tr.is_instead_of_trigger AS instead, tr.is_disabled AS disabled,
                   (SELECT STRING_AGG(te.type_desc, ',') FROM sys.trigger_events te
                     WHERE te.object_id = tr.object_id) AS events,
                   m.definition AS code
            FROM sys.triggers tr LEFT JOIN sys.sql_modules m ON m.object_id = tr.object_id
            WHERE tr.parent_class = 1 AND tr.is_ms_shipped = 0"""):
            out.append(SourceObject(source_id=self.source_id, engine=self.engine, kind="TRIGGER",
                                    schema_name=r["sch"], name=r["nm"], code=r["code"],
                                    meta={"on_schema": r["psch"], "on_table": r["ptbl"],
                                          "events": (r["events"] or "").split(","),
                                          "timing": "INSTEAD OF" if r["instead"] else "AFTER",
                                          "disabled": bool(r["disabled"])}))
        return out

    def close(self) -> None:
        self.conn.close()
