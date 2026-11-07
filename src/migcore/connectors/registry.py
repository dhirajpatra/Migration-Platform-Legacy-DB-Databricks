from __future__ import annotations
from migcore.connectors.base import SourceConnector


def build_connector(src: dict) -> SourceConnector:
    engine = src["engine"].lower()
    if engine == "sqlserver":
        from migcore.connectors.sqlserver import SqlServerConnector
        return SqlServerConnector(src["id"], src)
    if engine == "oracle":
        from migcore.connectors.oracle import OracleConnector
        return OracleConnector(src["id"], src)
    raise ValueError(f"No connector for engine '{engine}'. Add one in connectors/ and register it here.")
