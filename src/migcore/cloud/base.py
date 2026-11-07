"""Cloud provider contract. AWS and Azure implement the SAME process with different tools:

  secrets -> landing storage -> bulk/CDC extraction tool -> Databricks ingestion (Auto Loader/COPY INTO) -> Unity Catalog
"""
from __future__ import annotations
import json
import math
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

NUMERIC = {"int", "bigint", "smallint", "tinyint", "number", "numeric", "decimal", "integer"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", s.lower())


@dataclass
class TableLoad:
    source_id: str
    engine: str
    schema: str
    table: str
    landing_uri: str
    bronze_table: str            # catalog.schema.table in Unity Catalog
    partition_column: str | None
    partitions: int
    row_estimate: int | None


@dataclass
class LoadPlan:
    cloud: str
    catalog: str
    mode: str                    # full_load | full_load_and_cdc
    tables: list[TableLoad] = field(default_factory=list)
    tooling: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class SecretError(RuntimeError):
    pass


class CloudProvider(ABC):
    name: str
    tooling: dict

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.target = (cfg.get("engagement", {}) or {}).get("target", {}) or {}
        self.catalog = self.target.get("catalog", "migration_dev")
        self.pc = (cfg.get("cloud", {}) or {}).get(self.name, {}) or {}

    # ---------------------------------------------------------------- tool-specific (abstract)
    @abstractmethod
    def landing_uri(self, source_id: str, schema: str, table: str) -> str: ...
    @abstractmethod
    def _cloud_get_secret(self, name: str) -> str: ...
    @abstractmethod
    def put_object(self, key: str, data: bytes) -> str: ...
    @abstractmethod
    def list_objects(self, prefix: str) -> list[str]: ...
    @abstractmethod
    def render_artifacts(self, plan: LoadPlan, inventories: list[dict], out_dir: Path) -> list[Path]: ...
    @abstractmethod
    def uc_requirements(self) -> dict:
        """What Unity Catalog needs to read the landing zone (storage credential + external location)."""

    # ---------------------------------------------------------------- shared behaviour
    def get_secret(self, name: str) -> str:
        """Backend 'env' (default for dev/mock) or the cloud's secret manager. Values never get logged."""
        if self.pc.get("secrets_backend", "env") == "env":
            var = "SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
            if var not in os.environ:
                raise SecretError(f"secret '{name}' not found (env var {var})")
            return os.environ[var]
        return self._cloud_get_secret(name)

    def bronze_ref(self, source_id: str, schema: str, table: str) -> str:
        return f"{self.catalog}.bronze_{slug(source_id)}.{slug(schema)}_{slug(table)}"

    def build_plan(self, inventories: list[dict], mode: str = "full_load_and_cdc") -> LoadPlan:
        plan = LoadPlan(cloud=self.name, catalog=self.catalog, mode=mode, tooling=self.tooling)
        for inv in inventories:
            for t in inv["tables"]:
                pk = t.get("pk") or []
                col = next((c for c in t["columns"] if len(pk) == 1 and c["name"] == pk[0]), None)
                rows = t.get("row_estimate")
                numeric_pk = bool(col and col["type"].lower() in NUMERIC)
                parts = max(1, min(32, math.ceil((rows or 0) / 1_000_000))) if numeric_pk else 1
                if parts == 1 and rows and rows > 1_000_000:
                    plan.notes.append(f"{inv['source_id']}.{t['schema']}.{t['name']}: ~{rows:,} rows but no single numeric PK; "
                                      "single-stream load. Consider ROWID/date-range partitioning.")
                plan.tables.append(TableLoad(
                    source_id=inv["source_id"], engine=inv["engine"], schema=t["schema"], table=t["name"],
                    landing_uri=self.landing_uri(inv["source_id"], t["schema"], t["name"]),
                    bronze_table=self.bronze_ref(inv["source_id"], t["schema"], t["name"]),
                    partition_column=pk[0] if numeric_pk else None, partitions=parts, row_estimate=rows))
        if mode == "full_load_and_cdc":
            plan.notes.append("CDC requires source-side enablement (SQL Server CDC/CT, Oracle supplemental logging/archivelog); "
                              "enabling it is a source change and needs the source owner's approval (Gate 3).")
        return plan

    def bronze_sql(self, plan: LoadPlan) -> str:
        """Identical on both clouds: Unity Catalog bronze tables + COPY INTO from the landing zone."""
        out = [f"-- Generated for {plan.cloud}. Bronze layer = raw landed copy (SKILL §3 Phase 4).",
               f"-- Requires UC external location covering the landing path: {json.dumps(self.uc_requirements()['external_location_url'])}"]
        for s in sorted({t.bronze_table.rsplit('.', 1)[0] for t in plan.tables}):
            out.append(f"CREATE SCHEMA IF NOT EXISTS {s};")
        for t in plan.tables:
            out.append(f"\nCREATE TABLE IF NOT EXISTS {t.bronze_table};")
            out.append(f"COPY INTO {t.bronze_table}\nFROM '{t.landing_uri}'\nFILEFORMAT = PARQUET\n"
                       f"COPY_OPTIONS ('mergeSchema' = 'true');")
        return "\n".join(out) + "\n"

    def write_all(self, inventories: list[dict], out_dir: Path, mode: str = "full_load_and_cdc") -> tuple[LoadPlan, list[Path]]:
        out_dir.mkdir(parents=True, exist_ok=True)
        plan = self.build_plan(inventories, mode)
        files = [out_dir / "load_plan.json", out_dir / "bronze_ingest.sql"]
        files[0].write_text(json.dumps(plan.to_dict(), indent=2))
        files[1].write_text(self.bronze_sql(plan))
        files += self.render_artifacts(plan, inventories, out_dir)
        return plan, files
