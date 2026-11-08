# Migration Platform — Legacy DB → Databricks

Multi-agent, lift-and-shift platform that discovers a legacy database estate (SQL Server, Oracle, others later), understands it
(AST pre-processing, dependency graph, RAG knowledge base), translates it, moves the data and validates parity — landing in
**Databricks (Delta + Unity Catalog)** on **AWS or Azure**. Humans approve at defined gates; deterministic tools do everything they can before any LLM is used.

> **Philosophy:** *deterministic first, LLM second, human at the gates.* Source access is read-only, always.

**Contents:** [Specs](#1-specs-skillmd-and-agentmd) · [Milestone 0](#2-milestone-0--discovery-to-gate-1) · [Milestone 1](#2b-milestone-1--target-design-schema--view-translation-verify) · [Cloud modules](#3-cloud-modules-aws-and-azure--mock-databricks-token) ·
[Run it](#4-quick-start) · [Tests](#5-tests) · [Layout](#6-layout) · [Status & limits](#7-honest-status-and-known-limits) · [Cost & scalability review](#7b-architecture-review--cost-and-scalability) · [Roadmap](#8-roadmap-and-open-decisions) · [Decision log](#9-decision-log)

---

## 1. Specs: `SKILL.md` and `AGENT.md`
Both files live in the repo root and are the source of truth for the design.

**SKILL.md — the domain playbook**
- **Pipeline:** discovery → AST pre-processing → graph analysis → target design → translation → data movement → validation → deployment, each phase with exit criteria and four human gates (G1 scope & waves, G2 target design, G3 data load, G4 prod release).
- **Catalog sources:** the SQL Server and Oracle catalog views to extract from, plus a read-only MCP connector contract so new engines are added without changing agents.
- **AST contract:** the IR (intermediate representation) schema, mandatory analyzers, and a round-trip parse invariant.
- **Neo4j model:** node/edge types, standard queries, Graph RAG retrieval.
- **Scoring:** complexity scoring and wave planning.
- **Translation:** layered strategy (deterministic transpile → rule-guided LLM → pattern library → human), targets for procs/functions/packages/triggers, a list of Oracle and T-SQL semantic traps, and a verification loop requiring *parse + `EXPLAIN` compile + ≥1 behavioural test*.
- **KB, security, validation:** RAG collections (only verified translations are written back), non-negotiable security rules (read-only source, no row data to LLMs, source code treated as untrusted input), a validation spec and a data-type mapping table.

**AGENT.md — the operating contract**
- **20 agent roles:** Supervisor, Scoping/Guardrail, Discovery, AST, Graph Builder, Analyzer/Planner, Target Designer; Schema, View, Code, Trigger, Job and Security translators; independent Critic; non-LLM Verifier; Data Migration, Validation, Deployment, Knowledge Curator; Observability/Cost controller. Each has tools (least privilege), rules and escalation conditions.
- **Contracts:** shared JSON envelope with idempotency keys and retry/budget caps, HITL gates, LangGraph flow, MCP tool inventory, prompting standards, evaluation plan (golden set), failure-handling matrix, rules for coding assistants working in this repo.

**Three design choices to keep in mind**
1. **Triggers** have no direct Databricks equivalent, so they are re-architected *by purpose* (Change Data Feed audit, constraints, pipeline steps), not translated line-by-line. Every conversion carries a behaviour-change note; cascading triggers always go to human review.
2. **Cursor / row-by-row logic** is redesigned set-based wherever possible, not ported as PySpark loops.
3. **LLM data handling:** schema and code may go to an LLM; **row data does not** unless a specific table is explicitly approved (important for client-data constraints on consulting engagements).

---

## 2. Milestone 0 — discovery to Gate 1
Runs end-to-end in Docker Compose:

```
guardrail → discovery → AST pre-processor → graph build (Neo4j) → analyzer → [Gate 1: human approval]
```

| Piece | What it does today |
|---|---|
| **Guardrail agent** | Connects with the read-only login and **blocks the run** if it finds any write-capable permission, or if a source is unreachable. |
| **Discovery agent** | Extracts tables, columns, PKs, FKs, views, procs, functions, packages (spec+body merged), triggers + code text from SQL Server and Oracle. Per-object `source_hash`. Encrypted modules are flagged, never bypassed. |
| **AST pre-processor** | Code → IR: tables read/written (with operations), calls, dynamic SQL (resolved vs unresolved), construct inventory, side effects, control-flow metrics, injection-in-comments flag. Resolved against the discovery catalog so unknown identifiers never become graph edges. |
| **Graph builder** | Idempotent `MERGE` load into Neo4j (`READS / WRITES / CALLS / FK_TO / FIRES_ON`). Analysis still runs if Neo4j is down (flagged as an issue). |
| **Analyzer** | Complexity score + bucket, hard-flag routing to humans (e.g. unresolved dynamic SQL), dependency **waves** (SCC-condensed, leaf-first), cycles, hotspots, dead-code *candidates*. Writes `assessment.md/json`. |
| **Gate 1** | LangGraph `interrupt`; resumes via `POST /runs/{id}/approve`. The decision is audit-logged (`audit.jsonl`). |
| **MCP servers** | `mcp-sqlserver`, `mcp-oracle` expose the same read-only contract (profile `mcp`). |

Demo estates (`seed/`): a SQL Server DB (`LegacyDemo`: tables, view, function, procs incl. cursor + dynamic SQL, trigger) and an Oracle schema (`DEMO`: tables, sequences, function, view with `(+)` join, package, trigger), each with a read-only login that has **no data access and no write rights**.


## 2b. Milestone 1 — target design, schema & view translation, verify
Continues the same run after Gate 1:

```
[G1 approved] → target design → [Gate 2: human approval] → schema + view translation → verifier → report
```

| Piece | What it does today |
|---|---|
| **Target Designer** | Proposes Unity Catalog naming (`<catalog>.<source>_<schema>.<object>`), managed Delta tables, liquid clustering on the first PK column for tables ≥ 1M rows, informational PK/FK policy. Written to `target/design.json`; a human approves it at **Gate 2**. |
| **Type rules** (`rules/type_maps.py`) | Deterministic SQL Server / Oracle → Databricks type mapping. Every lossy choice is **flagged** (e.g. `tinyint`→`SMALLINT` because it is unsigned; Oracle `DATE`→`TIMESTAMP`; Oracle `NUMBER` with no precision; `''`≡NULL; datetimeoffset). Unknown types map to `STRING` and are flagged **high**, never silently. |
| **Schema Translation** | Table DDL (`NOT NULL`, identity → `BIGINT GENERATED BY DEFAULT AS IDENTITY` so existing keys load as-is), PK/FK as informational constraints (FK columns now extracted from both engines). |
| **View Translation** | sqlglot transpile with **catalog-driven rewriting** of every table and function reference to Unity Catalog names. Dropped hints (e.g. `NOLOCK`) are disclosed as behaviour changes. Views that call functions are marked *blocked on dependency* (functions arrive in M2). |
| **Unsafe-construct policy** | Anything whose automatic translation can change meaning gets **no deployable artifact** and goes to `manual_required`. Example: Oracle `(+)` outer joins — a generic transpile silently produced an *inner* join, so the demo's `vw_customer_orders` is blocked on purpose. Also `CONNECT BY`, `OPENXML`, `FOR XML`, `ROWNUM`. |
| **Verifier** | **Mock token:** syntax check only → `syntax_verified` (compilation is *not* claimed). **Real token:** runs DDL/views in a **sandbox catalog** (`<catalog>_sandbox`, never the real catalog) → `compile_verified`. `auto_verified` is reserved for objects that also pass data-parity/behavioural tests (M3) — nothing is promoted to it yet. |

Outputs under `runs/<id>/target/`: `design.json`, `ddl/{schemas.sql,constraints.sql,tables/*.sql}`, `views/*.sql`,
`translation_report.{json,md}` (status, flags, rules applied, behaviour changes, blocked-on, per object).

Demo result (mock mode): 14 objects checked, 0 failed — 10 `syntax_verified`, 3 `needs_review` (two Oracle tables with `NUMBER` lacking precision, one view blocked on a function), 1 `manual_required` (the `(+)` view).

---

## 3. Cloud modules (AWS and Azure) + mock Databricks token
Same process on both clouds, different tools. Select with `TARGET_CLOUD=aws|azure` (or `?cloud=` per request).

| Step | AWS module (`cloud/aws.py`) | Azure module (`cloud/azure.py`) |
|---|---|---|
| Secrets | AWS Secrets Manager | Azure Key Vault |
| Landing zone | S3 `s3://` (MinIO in local dev) | ADLS Gen2 `abfss://` (Azurite in local dev, profile `azure`) |
| Bulk + CDC extraction | **AWS DMS** `full-load-and-cdc` → S3 Parquet | **Azure Data Factory** Copy (self-hosted IR for private sources); CDC via ADF CDC / Debezium (Oracle: GoldenGate/Debezium) |
| Databricks ingest | `COPY INTO` / Auto Loader from `s3://` | `COPY INTO` / Auto Loader from `abfss://` |
| UC access to landing | IAM role → storage credential | Access Connector managed identity → storage credential |
| IaC | `deploy/terraform/aws` | `deploy/terraform/azure` |

**What each module generates** from a run's discovery inventory — `POST /runs/{id}/cloud-plan?cloud=aws|azure`, or
`python -m migcore.cloud --run <id> --cloud azure` — under `runs/<id>/cloud/<cloud>/`:
- `load_plan.json` — per-table landing URI, bronze table, range partitioning for numeric single-column PKs; large tables without one are **flagged, not guessed**.
- `bronze_ingest.sql` — Unity Catalog schemas + `COPY INTO`, identical on both clouds.
- **AWS:** DMS source/target endpoint and task JSON. **Azure:** ADF linked services, datasets, pipelines, CDC options.
- Credentials are **referenced** (Secrets Manager id / Key Vault secret name), never embedded. Tests assert the path DMS/ADF write to equals the path Databricks reads.

**Mock Databricks token.** Default `DATABRICKS_TOKEN=dapi-mock-0000`. With a mock token — or no host — `DatabricksClient` makes **no network calls**: statements are
recorded and `explain()` proves *syntax only* (`compiled: null` = compilation not proven, so nothing can be marked verified against a mock).
Set a real `DATABRICKS_HOST`, `DATABRICKS_TOKEN` and `DATABRICKS_WAREHOUSE_ID` and it flips to the SQL Statement Execution API automatically.
`GET /target/status` shows the active cloud and mode.

**Secrets in dev:** `secrets_backend: env` reads `SECRET_<NAME>` variables; set `secrets_backend: cloud` for Secrets Manager / Key Vault.

---

## 4. Quick start
```bash
cp .env.example .env            # dev-only credentials (incl. mock Databricks token)
make up                         # platform + demo SQL Server + Oracle (profile: demo)
# wait for sqlserver-init to exit 0 and oracle to be healthy (first Oracle start ~1–2 min)
make run                        # -> {"run_id":"abc123..."}
make status RUN=abc123          # becomes awaiting_g1
make report RUN=abc123          # the Assessment Report
make approve RUN=abc123         # passes Gate 1 -> status becomes awaiting_g2
curl localhost:8000/runs/abc123/report/design.json          # the proposed target design
make approve RUN=abc123         # passes Gate 2 -> translation + verify -> status m1_done
curl localhost:8000/runs/abc123/report/translation_report.md

curl localhost:8000/target/status                              # active cloud + mock/real Databricks
curl -X POST "localhost:8000/runs/abc123/cloud-plan?cloud=azure"   # or cloud=aws
docker compose --profile azure up -d                           # optional: Azurite (Azure Storage emulator)
```
UIs: API docs http://localhost:8000/docs · Neo4j Browser http://localhost:7474 (`neo4j` / `NEO4J_PASSWORD`) · MinIO http://localhost:9001.
Try in Neo4j: `MATCH (p:Procedure)-[r:WRITES|CALLS|READS]->(x) RETURN p,r,x`

**Your own databases:** edit `config/engagement.yaml` (`sources:`), keep secrets in `.env`/a secret store, skip `--profile demo`.
Create read-only logins like the seeds do: SQL Server `VIEW DEFINITION` only; Oracle `CREATE SESSION` + `SELECT_CATALOG_ROLE`.

---

## 5. Tests
`make test` (no Docker needed) — **41 tests** run the whole graph against a seed-backed fake connector: AST extraction (T-SQL + PL/SQL), wave ordering,
hard-flag routing, Gate 1 pause/approve/reject, blocking on write-capable logins and unreachable sources, the HTTP API lifecycle,
both cloud modules (plans, DMS/ADF artifacts, storage ops via fakes, mock Databricks client), and Milestone 1: type rules, Gate 2 (approve/reject), DDL/identity/clustering/constraints, view rewriting, the blocked `(+)` view, and the verifier in mock mode and in real mode (against a fake warehouse, asserting nothing touches the real catalog).

---

## 6. Layout
```
SKILL.md  AGENT.md  README.md  docker-compose.yml  .env.example  Makefile  config/engagement.yaml
src/migcore/{connectors,ast,graph,agents,orchestrator,cloud,rules}   mcp_servers/server.py
deploy/terraform/{aws,azure}   seed/{sqlserver,oracle}   tests/
runs/<run_id>/{source,inventory,ir,graph_payload.json,assessment.*,audit.jsonl,cloud/<cloud>/...,target/...}
```

---

## 7. Honest status and known limits
**Verified:** the Python logic and the 41 tests; compose/config YAML structure; MCP server tool registration (5 tools).

**Not yet run / validated**
- **The containers themselves** (no Docker in the build sandbox). Likeliest first-run friction: the Oracle seed / `initdb.d` behaviour, catalog queries against real SQL Server and Oracle, and the Postgres checkpointer (falls back to in-memory with a warning, so runs would not survive a restart). Treat the first `make up` as the integration test.
- **Terraform** modules are starters — no Terraform binary was available; run `terraform init && terraform validate` first.
- **DMS / ADF JSON** follows the services' documented shapes but has not been imported into real DMS/ADF; expect small field fixes on first deploy.
- **Real data movement** (running DMS/ADF, enabling CDC) is Milestone 3 and sits behind Gate 3, because enabling CDC changes the source.

**Design limits**
- **Parsing is hybrid** (sqlglot + regex, labelled in `parse_status`). Fully structured procedural parsing (ANTLR T-SQL / PL-SQL grammars) is the next AST upgrade; cursor and dynamic-SQL edge cases are flagged, not guessed.
- Procedure→procedure edges are name-token matches against the catalog (recorded as `resolution: name_match`).
- The orchestrator calls connectors in-process; the MCP servers expose the same contract and become the transport in a later milestone.
- "Dead-code candidates" include legitimate entry points (called by apps/jobs); usage telemetry (Query Store / AWR) is needed to confirm. Nothing is ever dropped automatically.
- **No LLM is called yet** — by design, Milestone 0 is the deterministic foundation.
- With a mock Databricks token nothing can be marked compiled: syntax is proven, compilation is not. `auto_verified` additionally needs data parity (M3).
- **Real-warehouse verification path** (sandbox catalog, `compile_verified`) is tested only against a fake client; it has never run on a live workspace.
- **Column defaults are not carried over** (not extracted yet); indexes, check constraints and computed columns are not translated yet.
- Only **tables, constraints and views** are translated so far. Functions, procedures, packages, triggers and jobs are M2; views that call functions stay `needs_review` until then.
- The `(+)` outer-join rewrite is intentionally manual; a verified rule (with a behavioural test) is a good M2 addition.
- Databricks reserved-word quoting uses a conservative built-in list; confirm against your runtime's ANSI mode on first real run.

---

## 7b. Architecture review — cost and scalability
*Reasoned assessment, not measured: nothing here has been load-tested or priced against your workload.*

**Verdict:** a sound, cost-conscious starting structure for development and small/medium estates — not "the most" scalable or cheapest option, and not production-ready as is.

**What is right-sized**
- **Postgres + pgvector** does three jobs (LangGraph checkpoints, audit, vector KB), so there is no separate vector database to pay for or run. A migration KB is thousands to low hundreds of thousands of chunks, well inside pgvector's range.
- **Deterministic-first** keeps LLM spend low: parsing, scoring, type maps and most view rewrites cost nothing per call. Idempotency keys `(run, object, step, source_hash)` mean unchanged objects are never re-processed.
- **Backend scale needs are modest.** An estate is thousands to tens of thousands of objects. The heavy volume (TBs of data) flows through DMS/ADF/Databricks, not through this backend.

**What would not hold up in production (fix before go-live)**
1. **Run artifacts live on local disk (`runs/`).** Breaks with multiple workers and is lost on container restart. Move to object storage (S3/ADLS) plus Postgres (or Delta tables) for inventory, IR, results and audit.
2. **The orchestrator runs graph executions in threads inside the API process.** No horizontal scaling and no queue. Split into API + workers with a queue (SQS / Service Bus / Celery / Temporal) before M2, when per-object LLM calls need fan-out and rate limiting.
3. **Neo4j is optional, not load-bearing.** Analysis uses networkx on the same edge data; Neo4j adds Graph RAG retrieval and visual exploration. Community edition is free but single-instance; Aura/Enterprise cost real money. Keep it if Graph RAG is a goal; otherwise Postgres tables are enough.
4. **Docker Compose is dev only.** Production: AKS/Container Apps or EKS/ECS, managed Postgres, secrets in Key Vault / Secrets Manager (`secrets_backend: cloud`), private networking.

**Where the money actually goes (qualitative; no prices quoted)**
- **Databricks compute and data movement** dominate: initial loads, CDC, validation queries. Levers: serverless/job compute with autoscale and auto-terminate, right-sized parallel partitions, avoid copying data twice, validate by hash/aggregates before row-level diffs, run validation off-peak.
- **LLM tokens** (from M2): route by complexity bucket (small model for Simple, stronger model + critic for Complex), cache by `source_hash`, send only the narrow context, use batch endpoints for non-urgent work, cap retries and per-run budgets.
- **Platform infra** (small Postgres, a few containers, object storage) is minor by comparison.
- **Tool choice affects cost:** DMS vs ADF vs Debezium vs Databricks-native ingestion/federation have different per-hour/per-DIU/per-DBU cost profiles and CDC coverage. Evaluate with your table sizes and change rates before committing (open decision in §8).

**Scaling path:** stateless API → queue → N workers (deterministic workers and LLM workers scaled separately) → state in Postgres + object storage → optional Neo4j. Each step is additive; the agent code and contracts do not change.

---

## 8. Roadmap and open decisions
1. ~~**M1 Target design + Schema/View translation**~~ — **done** (mock-verified; real-warehouse verification path built, awaiting a live workspace) — Gate 2
2. **M2 Function/Procedure/Package/Trigger translation + RAG/KB (pgvector)** with Critic and the full verification loop; verified `(+)` join rule; unblock views that call functions
3. **M3 Data migration + validation** — extraction → S3/ADLS → Delta, CDC, parity reports — Gate 3
4. **M4 Deployment** — Databricks Asset Bundles + Terraform for AWS/Azure — Gate 4

**Open decisions**
- Which cloud is the **first real target** (AWS or Azure), and Databricks workspace details (host, token, warehouse id) to replace the mock.
- Target runtime for procedural code: Databricks SQL scripting/procedures vs PySpark notebooks (decide before M2).
- CDC tooling per source: DMS vs ADF vs Debezium (Oracle: GoldenGate/Debezium).

---

## 9. Decision log
- Docker Compose is the local development environment.
- Two cloud modules (AWS, Azure) share one process; only tools differ.
- Databricks token is a mock for now (`dapi-mock-0000`); real values flip the client to live mode.
- Milestone 1 policy: never emit SQL that is known to change meaning — unsafe constructs become `manual_required` with no artifact; dropped semantics (e.g. `NOLOCK`) are always disclosed.
- Verification statuses are honest: `syntax_verified` (mock) < `compile_verified` (real sandbox) < `auto_verified` (needs data parity, M3).
- Read-only source access is enforced at the permission level (guardrail blocks the run), not just by prompt.
- Row data never goes to an LLM without explicit per-table approval.
