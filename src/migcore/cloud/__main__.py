"""python -m migcore.cloud --run <run_id> [--cloud aws|azure] [--mode full_load|full_load_and_cdc]"""
from __future__ import annotations
import argparse
import json
from migcore.config import load_config, runs_dir
from migcore.cloud import get_provider
from migcore.cloud.databricks import DatabricksClient


def generate(run_id: str, cfg: dict, cloud: str | None = None, mode: str = "full_load_and_cdc") -> dict:
    run = runs_dir() / run_id
    invs = [json.loads(p.read_text()) for p in sorted((run / "inventory").glob("*.json")) if not p.name.endswith(".code.json")]
    if not invs:
        raise FileNotFoundError(f"no inventory under {run}/inventory — run discovery first")
    prov = get_provider(cfg, cloud)
    plan, files = prov.write_all(invs, run / "cloud" / prov.name, mode)
    db = DatabricksClient.from_env(cfg)
    syn = db.check_syntax((run / "cloud" / prov.name / "bronze_ingest.sql").read_text())
    return {"cloud": prov.name, "databricks_mode": db.mode, "tables": len(plan.tables), "sql_syntax_ok": syn["ok"],
            "files": [str(f.relative_to(run)) for f in files], "notes": plan.notes, "tooling": prov.tooling}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--cloud")
    ap.add_argument("--mode", default="full_load_and_cdc")
    a = ap.parse_args()
    print(json.dumps(generate(a.run, load_config(), a.cloud, a.mode), indent=2))
