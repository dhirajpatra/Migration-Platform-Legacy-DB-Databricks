from __future__ import annotations
from migcore.cloud.base import CloudProvider


def get_provider(cfg: dict, cloud: str | None = None, **kw) -> CloudProvider:
    cloud = (cloud or (cfg.get("engagement", {}) or {}).get("target", {}).get("cloud") or "").lower()
    if cloud == "aws":
        from migcore.cloud.aws import AwsProvider
        return AwsProvider(cfg, **kw)
    if cloud == "azure":
        from migcore.cloud.azure import AzureProvider
        return AzureProvider(cfg, **kw)
    raise ValueError(f"unknown cloud '{cloud}' (expected 'aws' or 'azure')")
