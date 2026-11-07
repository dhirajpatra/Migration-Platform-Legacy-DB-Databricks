# Migration Platform — Legacy DB → Databricks (Milestone 0)

Multi-agent lift-and-shift platform. Specs: `SKILL.md` (domain playbook) and `AGENT.md` (agent contracts).
**Milestone 0 delivers phases 0–3 of the pipeline plus Human Gate 1**, running end-to-end in Docker Compose:

```
guardrail → discovery → AST pre-processor → graph build (Neo4j) → analyzer → [Gate 1: human approval]
```

| Piece | What it does today |
|---|---|
| **Guardrail agent** | Connects with the read-only login and **blocks the run** if it finds any write-capable permission (or the source is unreachable). |
| **Discovery agent** | Extracts tables, columns, PKs, FKs, views, procs, functions, packages (spec+body merged), triggers + code text from SQL Server and Oracle. Per-object `source_hash`. Encrypted modules are flagged, never bypassed. |
| **AST pre-processor** | Code → IR: tables read/written (with ops), calls, dynamic SQL (resolved vs unresolved), construct inventory, side effects, control-flow metrics, injection-in-comments flag. Resolved against the discovery catalog so unknown identifiers never become graph edges. |
| **Graph builder** | Idempotent `MERGE` load into Neo4j (`READS / WRITES / CALLS / FK_TO / FIRES_ON`). Analysis still runs if Neo4j is down (flagged as an issue). |
| **Analyzer** | Complexity score + bucket, hard-flag routing to humans, dependency **waves** (SCC-condensed, leaf-first), cycles, hotspots, dead-code *candidates*. Writes `assessment.md/json`. |
| **Gate 1** | LangGraph `interrupt`; resumes via `POST /runs/{id}/approve`. Decision is audit-logged. |
| **MCP servers** | `mcp-sqlserver`, `mcp-oracle` expose the same read-only contract (profile `mcp`). |

## Quick start
```bash
cp .env.example .env            # dev-only credentials
make up                         # builds + starts platform + demo SQL Server + Oracle (profile: demo)
# wait for sqlserver-init to exit 0 and oracle to be healthy (first Oracle start takes ~1-2 min)
make run                        # -> {"run_id":"abc123..."}
make status RUN=abc123          # status becomes awaiting_g1
make report RUN=abc123          # the Assessment Report
make approve RUN=abc123         # passes Gate 1
```
UIs: API docs http://localhost:8000/docs · Neo4j Browser http://localhost:7474 (`neo4j` / `NEO4J_PASSWORD`) · MinIO http://localhost:9001.
Try in Neo4j: `MATCH (p:Procedure)-[r:WRITES|CALLS|READS]->(x) RETURN p,r,x`

Point at **your own** databases: edit `config/engagement.yaml` (`sources:`), keep secrets in `.env`/secret store, skip `--profile demo`.
Create read-only logins like the demo seeds do (`seed/`): SQL Server `VIEW DEFINITION` only; Oracle `CREATE SESSION` + `SELECT_CATALOG_ROLE`.

## Tests (no Docker needed)
`make test` — 11 tests run the whole graph against a seed-backed fake connector: AST extraction (T-SQL + PL/SQL), wave ordering,
hard-flag routing, Gate 1 pause/approve/reject, blocking on write-capable logins and unreachable sources, and the HTTP API lifecycle.

## Layout
```
docker-compose.yml  .env.example  Makefile  config/engagement.yaml
src/migcore/{connectors,ast,graph,agents,orchestrator}   mcp_servers/server.py
seed/{sqlserver,oracle}   tests/   runs/<run_id>/{source,inventory,ir,graph_payload.json,assessment.*,audit.jsonl}
```

## Honest status / known limits
- **Verified here:** Python logic and tests above, compose YAML structure, MCP server tool registration.
- **Not yet run:** the containers themselves (no Docker in my build sandbox). Most likely first-run friction: Oracle seed/`initdb.d` behaviour,
  SQL Server catalog queries on real data, and the Postgres checkpointer (falls back to in-memory with a warning, so runs would not survive a restart).
  Treat the first `make up` as the integration test and send me the logs.
- **Parsing is hybrid** (sqlglot + regex, labelled in `parse_status`). Fully structured procedural parsing (ANTLR T-SQL/PL-SQL) is the next AST upgrade; cursor/dynamic-SQL edge cases are flagged, not guessed.
- Procedure→procedure edges are name-token matches against the catalog (recorded as `resolution: name_match`).
- Orchestrator currently calls connectors in-process; MCP servers exist for the same contract and become the transport in a later milestone.
- "Dead-code candidates" include legitimate entry points (called by apps/jobs) — usage telemetry (Query Store / AWR) is needed to confirm.
- No LLM is called yet — by design, milestone 0 is the deterministic foundation.

## Roadmap
1. **M1 Target design + Schema/View translation** (type maps, UC layout, `EXPLAIN` verifier against a sandbox Databricks SQL warehouse) — Gate 2
2. **M2 Code/Trigger translation + RAG/KB (pgvector)** with Critic and the verification loop
3. **M3 Data migration + validation** (JDBC → S3/ADLS → Delta, CDC, parity reports) — Gate 3
4. **M4 Deployment** (Databricks Asset Bundles + Terraform for AWS/Azure) — Gate 4
