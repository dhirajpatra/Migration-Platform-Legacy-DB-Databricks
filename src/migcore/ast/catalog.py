"""Name-resolution catalog built from the discovery inventory (per source)."""
from __future__ import annotations
from collections import defaultdict
from migcore.models import SourceObject

CALLABLE_KINDS = {"PROCEDURE", "FUNCTION", "PACKAGE"}
RELATION_KINDS = {"TABLE", "VIEW"}


class Catalog:
    def __init__(self, source_id: str):
        self.source_id = source_id
        self.relations: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # name -> [(fqn, schema, kind)]
        self.callables: dict[str, list[tuple[str, str, str]]] = defaultdict(list)

    def add(self, kind: str, schema: str, name: str) -> None:
        fqn = f"{self.source_id}.{schema}.{name}".lower()
        entry = (fqn, schema.lower(), kind)
        if kind in RELATION_KINDS:
            self.relations[name.lower()].append(entry)
        elif kind in CALLABLE_KINDS:
            self.callables[name.lower()].append(entry)

    @classmethod
    def from_inventory(cls, source_id: str, tables: list[dict], code_objects: list[SourceObject]) -> "Catalog":
        c = cls(source_id)
        for t in tables:
            c.add("TABLE", t["schema"], t["name"])
        for o in code_objects:
            c.add(o.kind, o.schema_name, o.name)
        return c

    def resolve_relation(self, schema: str | None, name: str, from_schema: str) -> tuple[str | None, str]:
        """-> (fqn|None, reason). reason in {ok, unknown, ambiguous}"""
        cands = self.relations.get(name.lower(), [])
        if not cands:
            return None, "unknown"
        if schema:
            m = [c for c in cands if c[1] == schema.lower()]
            return (m[0][0], "ok") if m else (None, "unknown")
        same = [c for c in cands if c[1] == from_schema.lower()]
        if same:
            return same[0][0], "ok"
        return (cands[0][0], "ok") if len(cands) == 1 else (None, "ambiguous")
