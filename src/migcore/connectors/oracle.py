from __future__ import annotations
import re
from collections import defaultdict
from migcore.connectors.base import SourceConnector
from migcore.models import SourceObject

SAFE_PRIVS = {"CREATE SESSION"}
BAD_ROLES = {"DBA", "RESOURCE", "SYSDBA"}
SRC_KIND = {"PROCEDURE": "PROCEDURE", "FUNCTION": "FUNCTION", "PACKAGE": "PACKAGE",
            "PACKAGE BODY": "PACKAGE_BODY"}


class OracleConnector(SourceConnector):
    engine = "oracle"

    def __init__(self, source_id: str, cfg: dict):
        super().__init__(source_id, cfg)
        import oracledb  # thin mode: no Oracle client install needed
        self.conn = oracledb.connect(user=cfg["user"], password=cfg["password"],
                                     dsn=f"{cfg['host']}:{cfg.get('port', 1521)}/{cfg['service']}")
        self.owners = [o.upper() for o in cfg.get("schemas", [])]

    def _q(self, sql: str) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute(sql)
        cols = [d[0].lower() for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        return rows

    def _owner_clause(self, col: str = "owner") -> str:
        if self.owners:
            return f"{col} IN ({','.join(repr(o) for o in self.owners)})"
        return f"{col} IN (SELECT username FROM dba_users WHERE oracle_maintained = 'N')"

    def check_readonly(self) -> list[str]:
        privs = {r["privilege"] for r in self._q("SELECT privilege FROM session_privs")}
        roles = {r["granted_role"] for r in self._q("SELECT granted_role FROM session_roles")}
        return sorted((privs - SAFE_PRIVS) | (roles & BAD_ROLES))

    def list_tables(self) -> list[dict]:
        cols = self._q(f"""
            SELECT c.owner, c.table_name, c.column_id, c.column_name, c.data_type, c.data_length,
                   c.data_precision, c.data_scale, c.nullable, t.num_rows
            FROM dba_tab_columns c JOIN dba_tables t ON t.owner = c.owner AND t.table_name = c.table_name
            WHERE {self._owner_clause('c.owner')} ORDER BY c.owner, c.table_name, c.column_id""")
        pks = self._q(f"""
            SELECT cc.owner, cc.table_name, cc.column_name, cc.position
            FROM dba_constraints k JOIN dba_cons_columns cc
              ON cc.owner = k.owner AND cc.constraint_name = k.constraint_name
            WHERE k.constraint_type = 'P' AND {self._owner_clause('k.owner')}
            ORDER BY cc.owner, cc.table_name, cc.position""")
        tables: dict[tuple, dict] = {}
        for r in cols:
            t = tables.setdefault((r["owner"], r["table_name"]), {
                "schema": r["owner"], "name": r["table_name"], "columns": [], "pk": [],
                "row_estimate": r["num_rows"]})
            t["columns"].append({"name": r["column_name"], "type": r["data_type"],
                                 "length": r["data_length"], "precision": r["data_precision"],
                                 "scale": r["data_scale"], "nullable": r["nullable"] == "Y",
                                 "identity": False})
        for r in pks:
            if (r["owner"], r["table_name"]) in tables:
                tables[(r["owner"], r["table_name"])]["pk"].append(r["column_name"])
        return list(tables.values())

    def list_foreign_keys(self) -> list[dict]:
        fks: dict[tuple, dict] = {}
        for r in self._q(f"""
            SELECT f.constraint_name, f.owner, f.table_name, p.owner AS r_owner, p.table_name AS ref_table,
                   fc.column_name AS col, pc.column_name AS rcol, fc.position
            FROM dba_constraints f
            JOIN dba_constraints p ON p.owner = f.r_owner AND p.constraint_name = f.r_constraint_name
            JOIN dba_cons_columns fc ON fc.owner = f.owner AND fc.constraint_name = f.constraint_name
            JOIN dba_cons_columns pc ON pc.owner = p.owner AND pc.constraint_name = p.constraint_name
                                    AND pc.position = fc.position
            WHERE f.constraint_type = 'R' AND {self._owner_clause('f.owner')}
            ORDER BY f.owner, f.constraint_name, fc.position"""):
            k = (r["owner"], r["table_name"], r["constraint_name"])
            e = fks.setdefault(k, {"name": r["constraint_name"], "schema": r["owner"], "table": r["table_name"],
                                   "ref_schema": r["r_owner"], "ref_table": r["ref_table"], "columns": [], "ref_columns": []})
            e["columns"].append(r["col"])
            e["ref_columns"].append(r["rcol"])
        return list(fks.values())

    def list_code_objects(self) -> list[SourceObject]:
        out: list[SourceObject] = []
        lines: dict[tuple, list[str]] = defaultdict(list)
        for r in self._q(f"""
            SELECT owner, name, type, line, text FROM dba_source
            WHERE {self._owner_clause()} AND type IN ('PROCEDURE','FUNCTION','PACKAGE','PACKAGE BODY')
            ORDER BY owner, name, type, line"""):
            lines[(r["owner"], r["name"], r["type"])].append(r["text"])
        for (owner, name, typ), txt in lines.items():
            out.append(SourceObject(source_id=self.source_id, engine=self.engine, kind=SRC_KIND[typ],
                                    schema_name=owner, name=name,
                                    code="CREATE OR REPLACE " + "".join(txt)))
        for r in self._q(f"SELECT owner, view_name, text FROM dba_views WHERE {self._owner_clause()}"):
            out.append(SourceObject(source_id=self.source_id, engine=self.engine, kind="VIEW",
                                    schema_name=r["owner"], name=r["view_name"],
                                    code=f"CREATE OR REPLACE VIEW {r['owner']}.{r['view_name']} AS\n{r['text']}"))
        for r in self._q(f"""
            SELECT owner, trigger_name, trigger_type, triggering_event, table_owner, table_name,
                   status, description, when_clause, trigger_body
            FROM dba_triggers WHERE {self._owner_clause()} AND base_object_type = 'TABLE'"""):
            when = f"WHEN ({r['when_clause']})\n" if r["when_clause"] else ""
            out.append(SourceObject(source_id=self.source_id, engine=self.engine, kind="TRIGGER",
                                    schema_name=r["owner"], name=r["trigger_name"],
                                    code=f"CREATE OR REPLACE TRIGGER {r['description']}{when}{r['trigger_body']}",
                                    meta={"on_schema": r["table_owner"], "on_table": r["table_name"],
                                          "events": [e.strip() for e in re.split(r"\bOR\b", r["triggering_event"] or "")],
                                          "timing": r["trigger_type"], "disabled": r["status"] != "ENABLED"}))
        return out

    def close(self) -> None:
        self.conn.close()
