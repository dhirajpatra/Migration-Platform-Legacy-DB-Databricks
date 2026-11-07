"""AWS module: Secrets Manager + S3 + AWS DMS (full load + CDC) -> Databricks Auto Loader / COPY INTO."""
from __future__ import annotations
import json
import os
from pathlib import Path
from migcore.cloud.base import CloudProvider, LoadPlan, slug

DMS_ENGINE = {"sqlserver": ("sqlserver", "MicrosoftSQLServerSettings"), "oracle": ("oracle", "OracleSettings")}


class AwsProvider(CloudProvider):
    name = "aws"
    tooling = {
        "secrets": "AWS Secrets Manager",
        "landing_storage": "Amazon S3 (s3://)",
        "bulk_and_cdc": "AWS DMS (full-load-and-cdc) -> S3 Parquet",
        "databricks_ingest": "Auto Loader / COPY INTO from s3://",
        "uc_identity": "IAM role registered as Unity Catalog storage credential",
        "network": "PrivateLink / VPC peering to sources; S3 gateway endpoint",
        "iac": "Terraform (aws + databricks providers) — deploy/terraform/aws",
    }

    def __init__(self, cfg: dict, s3_client=None):
        super().__init__(cfg)
        self.bucket = self.pc.get("bucket") or os.environ.get("S3_BUCKET", "migration-landing")
        self.prefix = (self.pc.get("landing_prefix") or "landing").strip("/")
        self.region = self.pc.get("region", "ap-south-1")
        self._s3 = s3_client

    # -- storage / secrets ---------------------------------------------------------------
    def landing_uri(self, source_id, schema, table):
        return f"s3://{self.bucket}/{self.prefix}/{source_id}/{schema}/{table}/"

    def _client(self, svc: str):
        import boto3  # lazy
        kw = {"region_name": self.region}
        if self.pc.get("endpoint_url") or os.environ.get("S3_ENDPOINT"):
            if svc == "s3":
                kw["endpoint_url"] = self.pc.get("endpoint_url") or os.environ["S3_ENDPOINT"]   # MinIO for local dev
        return boto3.client(svc, **kw)

    def _cloud_get_secret(self, name):
        sm = self._client("secretsmanager")
        return sm.get_secret_value(SecretId=f"{self.pc.get('secrets_prefix', 'migration')}/{name}")["SecretString"]

    def put_object(self, key, data):
        s3 = self._s3 or self._client("s3")
        s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return f"s3://{self.bucket}/{key}"

    def list_objects(self, prefix):
        s3 = self._s3 or self._client("s3")
        out, token = [], None
        while True:
            r = s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
            out += [o["Key"] for o in r.get("Contents", [])]
            token = r.get("NextContinuationToken")
            if not token:
                return out

    def uc_requirements(self):
        return {"external_location_url": f"s3://{self.bucket}/{self.prefix}/",
                "storage_credential": "IAM role (trust: Databricks UC master role + self-assume), s3:GetObject/ListBucket on the landing prefix",
                "terraform": "deploy/terraform/aws"}

    # -- AWS DMS artifacts ---------------------------------------------------------------
    def render_artifacts(self, plan: LoadPlan, inventories: list[dict], out_dir: Path) -> list[Path]:
        d = out_dir / "dms"
        d.mkdir(parents=True, exist_ok=True)
        pfx = self.pc.get("resource_prefix", "mig")
        access_role = self.pc.get("dms_secrets_access_role_arn", "arn:aws:iam::<ACCOUNT_ID>:role/<DMS_SECRETS_ACCESS_ROLE>")
        s3_role = self.pc.get("dms_s3_role_arn", "arn:aws:iam::<ACCOUNT_ID>:role/<DMS_S3_ACCESS_ROLE>")
        files: list[Path] = []
        for inv in inventories:
            sid, engine = inv["source_id"], inv["engine"]
            eng, key = DMS_ENGINE[engine]
            src = {"EndpointIdentifier": f"{pfx}-{slug(sid)}-src", "EndpointType": "source", "EngineName": eng,
                   key: {"SecretsManagerSecretId": f"{self.pc.get('secrets_prefix', 'migration')}/{sid}",
                         "SecretsManagerAccessRoleArn": access_role,
                         **({"DatabaseName": self.cfg_source(inv, "database")} if engine == "sqlserver" else {})}}
            tgt = {"EndpointIdentifier": f"{pfx}-{slug(sid)}-s3", "EndpointType": "target", "EngineName": "s3",
                   "S3Settings": {"BucketName": self.bucket, "BucketFolder": f"{self.prefix}/{sid}", "ServiceAccessRoleArn": s3_role,
                                  "DataFormat": "parquet", "ParquetVersion": "parquet-2-0", "EncodingType": "rle-dictionary",
                                  "IncludeOpForFullLoad": True, "CdcPath": "cdc", "TimestampColumnName": "_dms_ts"}}
            mappings = {"rules": [{"rule-type": "selection", "rule-id": str(i + 1), "rule-name": f"sel-{i + 1}",
                                   "object-locator": {"schema-name": t.schema, "table-name": t.table}, "rule-action": "include"}
                                  for i, t in enumerate(x for x in plan.tables if x.source_id == sid)]}
            task = {"ReplicationTaskIdentifier": f"{pfx}-{slug(sid)}-task",
                    "MigrationType": "full-load-and-cdc" if plan.mode == "full_load_and_cdc" else "full-load",
                    "SourceEndpointArn": f"<ARN of {src['EndpointIdentifier']}>", "TargetEndpointArn": f"<ARN of {tgt['EndpointIdentifier']}>",
                    "ReplicationInstanceArn": "<ARN of DMS replication instance in a subnet with private route to the source>",
                    "TableMappings": json.dumps(mappings), "ReplicationTaskSettings": json.dumps(
                        {"TargetMetadata": {"ParallelLoadThreads": 4}, "FullLoadSettings": {"MaxFullLoadSubTasks": 8}})}
            for fn, body in ((f"source_endpoint_{sid}.json", src), (f"target_endpoint_{sid}.json", tgt), (f"task_{sid}.json", task)):
                (d / fn).write_text(json.dumps(body, indent=2))
                files.append(d / fn)
        return files

    def cfg_source(self, inv: dict, key: str) -> str:
        for s in self.cfg.get("sources", []):
            if s.get("id") == inv["source_id"]:
                return str(s.get(key, ""))
        return ""
