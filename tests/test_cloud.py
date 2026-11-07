import json
import time
import pytest
from fastapi.testclient import TestClient
from fake_connector import SeedConnector
from migcore.cloud import get_provider
from migcore.cloud.base import SecretError
from migcore.cloud.databricks import DatabricksClient
from migcore.orchestrator.api import create_app

CFG = {
    "engagement": {"target": {"cloud": "aws", "catalog": "migration_dev"}},
    "cloud": {"aws": {"bucket": "land", "landing_prefix": "landing", "region": "ap-south-1", "secrets_prefix": "migration", "resource_prefix": "mig"},
              "azure": {"storage_account": "acct", "container": "land", "landing_prefix": "landing", "key_vault_url": "https://kv.vault.azure.net/"}},
    "sources": [{"id": "mssql_demo", "engine": "sqlserver", "database": "LegacyDemo"}, {"id": "oracle_demo", "engine": "oracle"}],
}


def col(name, typ):
    return {"name": name, "type": typ, "length": None, "precision": None, "scale": None, "nullable": False, "identity": False}


INVS = [
    {"source_id": "mssql_demo", "engine": "sqlserver", "foreign_keys": [], "objects": [], "tables": [
        {"schema": "dbo", "name": "Orders", "columns": [col("OrderId", "int"), col("Status", "varchar")], "pk": ["OrderId"], "row_estimate": 3_500_000},
        {"schema": "dbo", "name": "BigNoPk", "columns": [col("g", "uniqueidentifier")], "pk": ["g"], "row_estimate": 9_000_000},
        {"schema": "dbo", "name": "Small", "columns": [col("Id", "int")], "pk": ["Id"], "row_estimate": 10}]},
    {"source_id": "oracle_demo", "engine": "oracle", "foreign_keys": [], "objects": [], "tables": [
        {"schema": "DEMO", "name": "ORDERS", "columns": [col("ORDER_ID", "NUMBER")], "pk": ["ORDER_ID"], "row_estimate": 100}]},
]


# ---------------------------------------------------------------- shared process, per-cloud tools
@pytest.mark.parametrize("cloud,scheme", [("aws", "s3://land/landing/"), ("azure", "abfss://land@acct.dfs.core.windows.net/landing/")])
def test_plan_is_identical_in_shape_on_both_clouds(cloud, scheme, tmp_path):
    p = get_provider(CFG, cloud)
    plan, files = p.write_all(INVS, tmp_path)
    by = {(t.source_id, t.table): t for t in plan.tables}
    assert by[("mssql_demo", "Orders")].landing_uri == f"{scheme}mssql_demo/dbo/Orders/"
    assert by[("mssql_demo", "Orders")].partitions == 4 and by[("mssql_demo", "Orders")].partition_column == "OrderId"
    assert by[("mssql_demo", "Small")].partitions == 1
    assert by[("mssql_demo", "BigNoPk")].partitions == 1 and any("BigNoPk" in n for n in plan.notes)   # no numeric PK => flagged, not guessed
    assert by[("oracle_demo", "ORDERS")].bronze_table == "migration_dev.bronze_oracle_demo.demo_orders"
    assert any("CDC requires source-side enablement" in n for n in plan.notes)
    sql = (tmp_path / "bronze_ingest.sql").read_text()
    assert "COPY INTO migration_dev.bronze_mssql_demo.dbo_orders" in sql and scheme in sql
    assert DatabricksClient("", "dapi-mock-0000").check_syntax(sql)["ok"]


def test_aws_dms_artifacts(tmp_path):
    p = get_provider(CFG, "aws")
    plan, files = p.write_all(INVS, tmp_path)
    names = {f.name for f in files}
    assert {"task_mssql_demo.json", "source_endpoint_oracle_demo.json", "target_endpoint_mssql_demo.json"} <= names
    task = json.loads((tmp_path / "dms" / "task_mssql_demo.json").read_text())
    rules = json.loads(task["TableMappings"])["rules"]
    assert task["MigrationType"] == "full-load-and-cdc" and len(rules) == 3
    tgt = json.loads((tmp_path / "dms" / "target_endpoint_mssql_demo.json").read_text())["S3Settings"]
    # DMS lays out s3://bucket/<BucketFolder>/<schema>/<table>/ — must equal the landing URI Databricks reads
    assert f"s3://{tgt['BucketName']}/{tgt['BucketFolder']}/dbo/Orders/" == p.landing_uri("mssql_demo", "dbo", "Orders")
    src = json.loads((tmp_path / "dms" / "source_endpoint_mssql_demo.json").read_text())
    assert src["MicrosoftSQLServerSettings"]["SecretsManagerSecretId"] == "migration/mssql_demo"
    assert "password" not in json.dumps(src).lower()                      # credentials by reference only


def test_aws_full_load_only(tmp_path):
    p = get_provider(CFG, "aws")
    p.write_all(INVS, tmp_path, mode="full_load")
    assert json.loads((tmp_path / "dms" / "task_oracle_demo.json").read_text())["MigrationType"] == "full-load"


def test_azure_adf_artifacts(tmp_path):
    p = get_provider(CFG, "azure")
    p.write_all(INVS, tmp_path)
    pipe = json.loads((tmp_path / "adf" / "pipeline_mssql_demo.json").read_text())
    acts = {a["name"]: a for a in pipe["properties"]["activities"]}
    orders = next(a for n, a in acts.items() if n.endswith("dbo_orders"))
    assert orders["typeProperties"]["source"]["partitionOption"] == "DynamicRange" and orders["typeProperties"]["parallelCopies"] == 4
    small = next(a for n, a in acts.items() if n.endswith("dbo_small"))
    assert "partitionOption" not in small["typeProperties"]["source"]
    ls = {s["name"]: s for s in json.loads((tmp_path / "adf" / "linked_services.json").read_text())}
    assert ls["ls_mssql_demo"]["properties"]["typeProperties"]["connectionString"]["type"] == "AzureKeyVaultSecret"
    ds = {d["name"]: d for d in json.loads((tmp_path / "adf" / "datasets.json").read_text())}
    loc = next(v for k, v in ds.items() if k.startswith("ds_sink") and k.endswith("dbo_orders"))["properties"]["typeProperties"]["location"]
    assert f"abfss://{loc['fileSystem']}@acct.dfs.core.windows.net/{loc['folderPath']}/" == p.landing_uri("mssql_demo", "dbo", "Orders")
    assert "Oracle" in (tmp_path / "adf" / "cdc_options.json").read_text()


def test_factory_rejects_unknown_cloud():
    with pytest.raises(ValueError):
        get_provider(CFG, "gcp")


# ---------------------------------------------------------------- secrets + storage
def test_env_secret_backend(monkeypatch):
    p = get_provider(CFG, "azure")
    monkeypatch.setenv("SECRET_MSSQL_DEMO_PASSWORD", "pw")
    assert p.get_secret("mssql_demo_password") == "pw"
    with pytest.raises(SecretError):
        p.get_secret("nope")


def test_aws_storage_ops_with_fake_s3():
    class FakeS3:
        def __init__(self): self.d = {}
        def put_object(self, Bucket, Key, Body): self.d[Key] = Body
        def list_objects_v2(self, Bucket, Prefix, **kw): return {"Contents": [{"Key": k} for k in self.d if k.startswith(Prefix)]}
    p = get_provider(CFG, "aws", s3_client=FakeS3())
    assert p.put_object("landing/a/b.parquet", b"1") == "s3://land/landing/a/b.parquet"
    assert p.list_objects("landing/a") == ["landing/a/b.parquet"]


def test_azure_storage_ops_with_fake_blob():
    class Blob:
        def __init__(self, n): self.name = n
    class CC:
        def __init__(self, s): self.s = s
        def list_blobs(self, name_starts_with): return [Blob(k) for k in self.s if k.startswith(name_starts_with)]
    class BC:
        def __init__(self, s, k): self.s, self.k = s, k
        def upload_blob(self, data, overwrite): self.s.add(self.k)
    class Svc:
        def __init__(self): self.s = set()
        def get_blob_client(self, c, k): return BC(self.s, k)
        def get_container_client(self, c): return CC(self.s)
    p = get_provider(CFG, "azure", blob_service=Svc())
    assert p.put_object("landing/a/b.parquet", b"1").startswith("abfss://land@acct")
    assert p.list_objects("landing/") == ["landing/a/b.parquet"]


# ---------------------------------------------------------------- Databricks mock token
def test_databricks_mock_by_default_and_flips_to_real():
    assert DatabricksClient("", "dapi-mock-0000").mode == "mock"
    assert DatabricksClient("https://adb-1.azuredatabricks.net", "dapi-mock-0000").mode == "mock"   # real host but mock token
    assert DatabricksClient("https://adb-1.azuredatabricks.net", "dapi1234abcd").mode == "real"
    c = DatabricksClient("", "dapi-mock-0000")
    assert c.whoami()["mode"] == "mock"
    assert c.execute("SELECT 1")["status"] == "SUCCEEDED" and c.calls[0]["mode"] == "mock"


def test_mock_explain_catches_bad_sql_but_does_not_claim_compiled():
    c = DatabricksClient("", "dapi-mock-0000")
    good = c.explain("SELECT a FROM t")
    assert good["ok"] and good["compiled"] is None          # syntax proven, compile NOT proven (needs real warehouse)
    bad = c.explain("SELECT FROM WHERE")
    assert not bad["ok"] and bad["compiled"] is False


# ---------------------------------------------------------------- API
def test_api_target_status_and_cloud_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    cfg = {**CFG, "graph": {"enabled": False}, "sources": [{"id": "mssql_demo", "engine": "sqlserver"}]}
    c = TestClient(create_app(cfg, lambda s: SeedConnector(s["id"], s)))
    st = c.get("/target/status").json()
    assert st["cloud"] == "aws" and st["databricks"]["mode"] == "mock"
    rid = c.post("/runs").json()["run_id"]
    for _ in range(100):
        r = c.get(f"/runs/{rid}")
        if r.status_code == 200 and r.json()["status"] == "awaiting_g1":
            break
        time.sleep(0.1)
    out = c.post(f"/runs/{rid}/cloud-plan?cloud=azure").json()
    assert out["cloud"] == "azure" and out["databricks_mode"] == "mock" and out["sql_syntax_ok"] and out["tables"] == 5
    assert any(f.startswith("cloud/azure/adf/pipeline_") for f in out["files"])
    out2 = c.post(f"/runs/{rid}/cloud-plan?cloud=aws").json()
    assert any(f.startswith("cloud/aws/dms/task_") for f in out2["files"])
    assert c.post(f"/runs/{rid}/cloud-plan?cloud=gcp").status_code == 400
    assert c.post("/runs/nope/cloud-plan").status_code == 409
