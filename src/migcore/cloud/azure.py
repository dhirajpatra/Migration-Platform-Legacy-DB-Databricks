"""Azure module: Key Vault + ADLS Gen2 + Azure Data Factory (Copy; CDC via ADF CDC/Debezium) -> Databricks Auto Loader / COPY INTO."""
from __future__ import annotations
import json
import os
from pathlib import Path
from migcore.cloud.base import CloudProvider, LoadPlan, slug

ADF_SRC = {"sqlserver": ("SqlServer", "SqlServerSource", "SqlServerTable"), "oracle": ("Oracle", "OracleSource", "OracleTable")}


class AzureProvider(CloudProvider):
    name = "azure"
    tooling = {
        "secrets": "Azure Key Vault",
        "landing_storage": "ADLS Gen2 (abfss://)",
        "bulk_and_cdc": "Azure Data Factory Copy (self-hosted IR for private sources); CDC via ADF CDC resource or Debezium",
        "databricks_ingest": "Auto Loader / COPY INTO from abfss://",
        "uc_identity": "Databricks Access Connector (managed identity) as Unity Catalog storage credential",
        "network": "Private Endpoints / self-hosted integration runtime to sources",
        "iac": "Terraform (azurerm + databricks providers) — deploy/terraform/azure",
    }

    def __init__(self, cfg: dict, blob_service=None):
        super().__init__(cfg)
        self.account = self.pc.get("storage_account") or "devstoreaccount1"
        self.container = self.pc.get("container") or os.environ.get("S3_BUCKET", "migration-landing")
        self.prefix = (self.pc.get("landing_prefix") or "landing").strip("/")
        self.key_vault_url = self.pc.get("key_vault_url", "https://<your-vault>.vault.azure.net/")
        self._blob = blob_service

    def landing_uri(self, source_id, schema, table):
        return f"abfss://{self.container}@{self.account}.dfs.core.windows.net/{self.prefix}/{source_id}/{schema}/{table}/"

    def _blob_service(self):
        if self._blob:
            return self._blob
        from azure.storage.blob import BlobServiceClient  # lazy
        cs = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")       # Azurite in dev
        if cs:
            return BlobServiceClient.from_connection_string(cs)
        from azure.identity import DefaultAzureCredential
        return BlobServiceClient(f"https://{self.account}.blob.core.windows.net", credential=DefaultAzureCredential())

    def _cloud_get_secret(self, name):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
        return SecretClient(self.key_vault_url, DefaultAzureCredential()).get_secret(name.replace("/", "-").replace("_", "-")).value

    def put_object(self, key, data):
        self._blob_service().get_blob_client(self.container, key).upload_blob(data, overwrite=True)
        return f"abfss://{self.container}@{self.account}.dfs.core.windows.net/{key}"

    def list_objects(self, prefix):
        cc = self._blob_service().get_container_client(self.container)
        return [b.name for b in cc.list_blobs(name_starts_with=prefix)]

    def uc_requirements(self):
        return {"external_location_url": f"abfss://{self.container}@{self.account}.dfs.core.windows.net/{self.prefix}/",
                "storage_credential": "Databricks Access Connector managed identity with 'Storage Blob Data Contributor' on the storage account",
                "terraform": "deploy/terraform/azure"}

    def render_artifacts(self, plan: LoadPlan, inventories: list[dict], out_dir: Path) -> list[Path]:
        d = out_dir / "adf"
        d.mkdir(parents=True, exist_ok=True)
        ir = self.pc.get("integration_runtime", "AutoResolveIntegrationRuntime")
        kv_ref = {"referenceName": "ls_keyvault", "type": "LinkedServiceReference"}
        services = [
            {"name": "ls_keyvault", "properties": {"type": "AzureKeyVault", "typeProperties": {"baseUrl": self.key_vault_url}}},
            {"name": "ls_adls", "properties": {"type": "AzureBlobFS", "typeProperties": {"url": f"https://{self.account}.dfs.core.windows.net"},
                                               "connectVia": {"referenceName": "AutoResolveIntegrationRuntime", "type": "IntegrationRuntimeReference"}}}]
        datasets, files = [], []
        for inv in inventories:
            sid, engine = inv["source_id"], inv["engine"]
            ls_type, src_type, ds_type = ADF_SRC[engine]
            services.append({"name": f"ls_{slug(sid)}", "properties": {
                "type": ls_type, "connectVia": {"referenceName": ir, "type": "IntegrationRuntimeReference"},
                "typeProperties": {"connectionString": {"type": "AzureKeyVaultSecret", "store": kv_ref, "secretName": f"{sid}-connection-string"}}}})
            activities = []
            for t in (x for x in plan.tables if x.source_id == sid):
                base = f"{slug(sid)}_{slug(t.schema)}_{slug(t.table)}"
                datasets.append({"name": f"ds_src_{base}", "properties": {"type": ds_type, "linkedServiceName": {"referenceName": f"ls_{slug(sid)}", "type": "LinkedServiceReference"},
                                                                        "typeProperties": {"schema": t.schema, "table": t.table}}})
                datasets.append({"name": f"ds_sink_{base}", "properties": {"type": "Parquet", "linkedServiceName": {"referenceName": "ls_adls", "type": "LinkedServiceReference"},
                                                                         "typeProperties": {"location": {"type": "AzureBlobFSLocation", "fileSystem": self.container,
                                                                                                          "folderPath": f"{self.prefix}/{sid}/{t.schema}/{t.table}"}}}})
                source = {"type": src_type}
                if t.partitions > 1:
                    source.update({"partitionOption": "DynamicRange", "partitionSettings": {"partitionColumnName": t.partition_column}})
                tp = {"source": source, "sink": {"type": "ParquetSink", "storeSettings": {"type": "AzureBlobFSWriteSettings"},
                                                 "formatSettings": {"type": "ParquetWriteSettings"}}}
                if t.partitions > 1:
                    tp["parallelCopies"] = t.partitions
                activities.append({"name": f"copy_{base}"[:55], "type": "Copy", "typeProperties": tp,
                                   "inputs": [{"referenceName": f"ds_src_{base}", "type": "DatasetReference"}],
                                   "outputs": [{"referenceName": f"ds_sink_{base}", "type": "DatasetReference"}]})
            pipe = {"name": f"pl_bulk_{slug(sid)}", "properties": {"activities": activities, "concurrency": 1}}
            (d / f"pipeline_{sid}.json").write_text(json.dumps(pipe, indent=2))
            files.append(d / f"pipeline_{sid}.json")
        (d / "linked_services.json").write_text(json.dumps(services, indent=2))
        (d / "datasets.json").write_text(json.dumps(datasets, indent=2))
        cdc = {"mode": plan.mode, "sqlserver": "ADF Change Data Capture resource or Debezium SQL Server connector",
               "oracle": "Oracle GoldenGate or Debezium (LogMiner); ADF CDC does not cover Oracle"}
        (d / "cdc_options.json").write_text(json.dumps(cdc, indent=2))
        return files + [d / "linked_services.json", d / "datasets.json", d / "cdc_options.json"]
